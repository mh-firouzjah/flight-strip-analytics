from core.models import Strip
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Delete all records from the strips table."

    def handle(self, *args, **options):
        Strip.objects.all().delete()
        self.stdout.write(self.style.SUCCESS("Successfully cleared the strips database."))
