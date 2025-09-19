import logging
from time import perf_counter

from django.conf import settings
from django.contrib import admin, messages
from django.contrib.admin.models import LogEntry
from django.core.files.uploadedfile import UploadedFile
from django.db import connection, transaction
from django.forms import ValidationError
from django.http import HttpRequest
from django.shortcuts import redirect, render
from django.urls import path
from humanize import naturalsize, precisedelta

from .forms import ExcelUploadForm
from .handlers.db import reconcile_journeys, upsert_strips_from_df
from .handlers.excel import NoDataError, process_excel_bytes
from .models import AuditLog, Strip

logger = logging.getLogger(__name__)


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    "user action result timestamp details"

    list_display = ("timestamp", "user", "action", "result", "details")
    list_filter = ("action", "result")
    search_fields = ("details", "user__username")
    ordering = ("-timestamp",)

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_update_permission(self, request):
        return request.user.is_superuser

    def has_delete_permission(self, request: HttpRequest, obj=None):
        return request.user.is_superuser


@admin.register(Strip)
class StripAdmin(admin.ModelAdmin):
    change_list_template = "admin/core/strip/change_list.html"

    list_display = ("callsign", "date", "dep", "des", "sector")
    list_filter = ("date",)
    search_fields = ("callsign", "dep", "des")
    ordering = ("-date",)

    def has_add_permission(self, request):
        return request.user.is_superuser

    def has_update_permission(self, request):
        return request.user.is_superuser

    def has_delete_permission(self, request: HttpRequest, obj=None):
        return request.user.is_superuser

    def has_import_permission(self, request):
        return request.user.has_perm("core.import_strip")

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "import-excel/",
                self.admin_site.admin_view(self.import_excel_view),
                name=f"{self.opts.app_label}_{self.opts.model_name}_import_excel",
            ),
        ]
        return custom_urls + urls

    def import_excel_view(self, request: HttpRequest):
        if not self.has_import_permission(request):
            return self._deny_permission(request)

        form = ExcelUploadForm(request.POST or None, request.FILES or None)

        if request.method == "POST" and form.is_valid():
            return self._handle_upload(request, form)

        return self._render_form(request, {"form": form})

    # --- helpers ---
    def _deny_permission(self, request):
        messages.error(request, "You do not have permission to import strips.")
        return redirect("admin:core_strip_changelist")

    def _render_form(self, request: HttpRequest, context: dict):
        context = {
            **self.admin_site.each_context(request),
            **context,
            "title": "Import",
            "opts": self.opts,
            "fields_list": tuple(f.name for f in self.opts.fields if f.name != "id"),
        }
        return render(request, "admin/core/strip/upload_excel.html", context)

    def _handle_upload(self, request: HttpRequest, form):
        year = form.cleaned_data["year"]
        excel_file = request.FILES.get("excel_file")

        start_time = perf_counter()
        try:
            excel_file = self._validate_file(excel_file)
            excel_bytes = excel_file.read()
            filename = excel_file.name
            filesize = excel_file.size
            elapsed = perf_counter() - start_time

            valid_df, errors_df = process_excel_bytes(excel_bytes, year)
            if (valid_df is None or valid_df.is_empty()) and (errors_df is None or errors_df.is_empty()):
                return self._handle_no_effect_file(request, filename, filesize, elapsed)
            if errors_df is not None and not errors_df.is_empty():
                return self._handle_error_prone_file(request, form, filename, filesize, errors_df, elapsed)
            return self._process_data(request, valid_df, filename, filesize, start_time)
        except Exception as err:
            return self._handle_exception(request, excel_file, err, perf_counter() - start_time)

    def _validate_file(self, file: UploadedFile | None):
        if not file:
            raise ValidationError("No file uploaded.")
        if file.content_type not in settings.UPLOADFILE_ALLOWED_TYPES:
            raise ValidationError("Invalid file type.")
        if file.size > settings.DATA_UPLOAD_MAX_MEMORY_SIZE:
            raise ValidationError("File too large.")
        return file

    def _handle_no_effect_file(self, request, filename, filesize, elapsed):
        AuditLog.objects.create(
            user=request.user,
            action=AuditLog.Action.UPLOAD,
            result=AuditLog.ActionResult.NO_EFFECT,
            details={
                "filename": filename,
                "file_size": naturalsize(filesize, binary=True),
                "duration": precisedelta(elapsed),
                "note": "No valid or error rows",
            },
        )
        messages.warning(
            request, f"The file was processed, but contained no data to import. {precisedelta(elapsed)}."
        )
        return self._render_form(request, {"form": ExcelUploadForm()})

    def _handle_error_prone_file(self, request, form, filename, filesize, errors_df, elapsed):
        messages.warning(
            request,
            f"Import stopped due to errors in {errors_df.height} row(s), {precisedelta(elapsed)}. "
            f"Please review details below.",
        )

        AuditLog.objects.create(
            user=request.user,
            action=AuditLog.Action.UPLOAD,
            result=AuditLog.ActionResult.REJECTED,
            details={
                "filename": filename,
                "file_size": naturalsize(filesize, binary=True),
                "duration": precisedelta(elapsed),
                "error_rows_count": errors_df.height,
            },
        )
        context = {
            "errors_df": errors_df.iter_rows(named=False),
            "error_headers": errors_df.columns,
            "form": form,
        }
        return self._render_form(request, context)

    def _process_data(self, request, valid_df, filename, filesize, start_time):
        db_start_time = perf_counter()
        sec_up, upsert_df = reconcile_journeys(valid_df)
        with transaction.atomic(), connection.cursor() as cursor:
            db_op = upsert_strips_from_df(cursor, upsert_df)

        db_duration = perf_counter() - db_start_time
        total_duration = perf_counter() - start_time
        if db_op.unchanged == db_op.imported:
            messages.warning(
                request,
                "The file was processed, but contained no updated or new data. "
                f"{precisedelta(total_duration)}.",
            )
            AuditLog.objects.create(
                user=request.user,
                action=AuditLog.Action.UPLOAD,
                result=AuditLog.ActionResult.NO_EFFECT,
                details={
                    "filename": filename,
                    "file_size": naturalsize(filesize, binary=True),
                    "db_duration": precisedelta(db_duration),
                    "total_duration": precisedelta(total_duration),
                    "note": "No valid or error rows",
                },
            )
        else:
            messages.success(
                request,
                (
                    f"Import was successful whithin {precisedelta(total_duration)}. "
                    f"inserted: {db_op.inserted}. updated: {db_op.updated}. "
                    f"unchanged: {db_op.unchanged}. imported: {db_op.imported}. "
                    f"sector-end fixed: {sec_up}"
                ),
            )
            AuditLog.objects.create(
                user=request.user,
                action=AuditLog.Action.UPLOAD,
                result=AuditLog.ActionResult.SUCCESS,
                details={
                    "filename": filename,
                    "file_size": naturalsize(filesize, binary=True),
                    "db_duration": precisedelta(db_duration),
                    "total_duration": precisedelta(total_duration),
                    "inserted": db_op.inserted,
                    "updated": db_op.updated,
                    "unchanged": db_op.unchanged,
                    "imported": db_op.imported,
                    "sector-end fixed": sec_up,
                },
            )

        return redirect("admin:core_strip_changelist")

    def _handle_exception(self, request, excel_file, err, elapsed):
        AuditLog.objects.create(
            user=request.user,
            action=AuditLog.Action.UPLOAD,
            result=AuditLog.ActionResult.ERROR,
            details={
                "filename": excel_file.name if excel_file else None,
                "file_size": naturalsize(excel_file.size if excel_file else 0, binary=True),
                "elapsed": precisedelta(elapsed),
                "error": str(err),
            },
        )

        if isinstance(err, NoDataError):
            messages.warning(request, f"{err}")
        else:
            messages.error(request, f"A critical error occurred during import. {err}")
            logger.exception("Critical error during import")
        return self._render_form(request, {"form": ExcelUploadForm()})


# Django default log model but in read only mode
@admin.register(LogEntry)
class LogEntryAdmin(admin.ModelAdmin):
    list_display = ("action_time", "user", "content_type", "object_repr", "action_flag", "change_message")
    list_filter = ("action_flag", "content_type")
    search_fields = ("object_repr", "change_message", "user__username")
    ordering = ("-action_time",)

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request: HttpRequest, obj=None):
        return False

    def has_delete_permission(self, request: HttpRequest, obj=None):
        return False

    def has_view_permission(self, request: HttpRequest, obj=None):
        return True
