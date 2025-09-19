from django.conf import settings
from django.db import models


class Strip(models.Model):
    # DB Index fields / Key Columns
    callsign = models.TextField(db_index=True)
    date = models.DateField(db_index=True)
    dep = models.TextField(db_index=True)
    des = models.TextField(db_index=True)
    sector = models.TextField(db_index=True)
    point1time = models.DateTimeField(db_index=True)
    sector_end = models.DateTimeField(db_index=True)
    journey_id = models.TextField(db_index=True)
    # Non DB Index fields / Non Key Columns
    ssrcode = models.TextField(blank=True, null=True)
    register = models.TextField(blank=True, null=True)
    aircraftname = models.TextField(blank=True, null=True)
    aircrafttype = models.TextField(blank=True, null=True)
    route = models.TextField(blank=True, null=True)
    point1 = models.TextField()
    point2 = models.TextField(blank=True, null=True)
    point2time = models.DateTimeField(blank=True, null=True)
    point3 = models.TextField(blank=True, null=True)
    point3time = models.DateTimeField(blank=True, null=True)
    point4 = models.TextField(blank=True, null=True)
    point4time = models.DateTimeField(blank=True, null=True)
    point5 = models.TextField(blank=True, null=True)
    point5time = models.DateTimeField(blank=True, null=True)
    point6 = models.TextField(blank=True, null=True)
    point6time = models.DateTimeField(blank=True, null=True)
    speed = models.TextField(blank=True, null=True)
    cfl = models.TextField(blank=True, null=True)
    rfl = models.TextField(blank=True, null=True)
    created = models.DateTimeField(auto_now_add=True)
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        permissions = [
            ("import_strip", "Can import strip"),
            ("import_outdated_strip", "Can import outdated strip"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["callsign", "date", "dep", "des", "sector"],
                name="unique_strip_sector_date",
            ),
            models.UniqueConstraint(
                fields=["journey_id", "sector"],
                name="unique_strip_journey_sector",
            ),
        ]

    def __str__(self):
        return f"{self.callsign} ({self.dep}-{self.des}) on {self.date}"


class AuditLog(models.Model):
    class Action(models.TextChoices):
        UPLOAD = "upload", "Upload"
        DELETE = "delete", "Delete"
        UPDATE = "update", "Update"

    class ActionResult(models.TextChoices):
        SUCCESS = "success", "Success"
        NO_EFFECT = "no_effect", "No Effect"
        REJECTED = "rejected", "Rejected"
        ERROR = "error", "Error"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="audit_logs",
        db_index=True,
        null=True,
        blank=True,
    )
    action = models.TextField(verbose_name="Action Type", choices=Action.choices, db_index=True)
    result = models.TextField(verbose_name="Action Result", choices=ActionResult.choices, db_index=True)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)
    details = models.JSONField(default=dict, blank=True, null=True)

    class Meta:
        ordering = ["-timestamp"]

    def __str__(self):
        actor = self.user.username if self.user else "System"
        return f"{self.get_action_display()} by {actor} at {self.timestamp.strftime('%Y-%m-%d %H:%M:%S')}"
