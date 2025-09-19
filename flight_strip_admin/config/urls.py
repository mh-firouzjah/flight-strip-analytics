from django.conf import settings
from django.contrib import admin
from django.urls import path

admin.site.site_header = "Flight Strip Administration"
admin.site.site_title = "FSA"
admin.site.index_title = "Manage Users, Permissions, Logs, and Data Uploads"

urlpatterns = [
    path("admin/", admin.site.urls),
]

if settings.DEBUG and not settings.TESTING:
    from debug_toolbar.toolbar import debug_toolbar_urls

    urlpatterns += debug_toolbar_urls()
