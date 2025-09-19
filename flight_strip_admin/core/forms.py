from datetime import date

from django import forms


class ExcelUploadForm(forms.Form):
    excel_file = forms.FileField(
        label="Select Excel file (.xlsx)",
        widget=forms.ClearableFileInput(attrs={"accept": ".xlsx"}),
    )
    year = forms.IntegerField(
        label="Data Year",
        min_value=2000,
        max_value=3000,
        initial=date.today().year,
        help_text="Year of the flight strips in the file (defaults to current year).",
    )
