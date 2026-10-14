"""
App Update Available Jobs

Notifies devices running an app version older than the currently published
"latest version" that an update is available. Unlike the other two
notification types (inactive_user, continue_journey), app_update_available
sends BOTH a push notification and an in-app notification record, per spec.

Latest version source: AppSetting rows, keyed by name (no dedicated table --
reuses the existing generic AppSetting table):
    name='latest_ios_version',     value='2.5.0'
    name='latest_android_version', value='2.5.0'
These rows are inserted/updated manually by an admin on each release; this
job only reads them.

Version comparison: uses packaging.version.parse (already a project
dependency -- confirmed installed before use) rather than a naive string or
hand-rolled tuple compare, so version strings sort correctly.

Dedup / re-notify policy: DeviceToken.app_update_notified_at is an
independent-per-device timestamp (NOT a one-time "ever notified" flag). A
device is eligible for a new notification if it is currently outdated
(app_version < latest_version for its platform) AND it has never been
notified before, OR at least 48 hours have passed since its last
app-update notification. This is the simplest cooldown design confirmed
with product -- no separate "last notified version" column is tracked.

Runs daily (app updates don't need hourly checks -- see jobs/__init__.py).
"""

import logging
from datetime import datetime, timedelta, timezone

from packaging.version import parse as parse_version, InvalidVersion

logger = logging.getLogger(__name__)

RENOTIFY_COOLDOWN = timedelta(hours=48)


def _get_latest_version(session, platform: str):
    """
    Read the latest published version for a platform ('ios' or 'android')
    from the AppSetting table (name='latest_ios_version' /
    'latest_android_version'). Returns None if not configured yet, so
    callers can skip that platform gracefully instead of erroring.
    """
    from services.models import AppSetting

    setting_name = f"latest_{platform}_version"
    row = (
        session.query(AppSetting)
        .filter(AppSetting.name == setting_name)
        .first()
    )
    if row is None or not row.value:
        return None
    return row.value


def _is_outdated(app_version: str, latest_version: str) -> bool:
    """
    Return True if app_version is strictly older than latest_version.
    Malformed version strings are treated as NOT outdated (skipped) rather
    than raising, so one bad app_version value can't kill the batch.
    """
    try:
        return parse_version(app_version) < parse_version(latest_version)
    except InvalidVersion:
        return False


def _should_notify(device, now: datetime) -> bool:
    """
    True if this device has never been notified about an app update, or
    it's been at least RENOTIFY_COOLDOWN since it last was.
    """
    if device.app_update_notified_at is None:
        return True
    return (now - device.app_update_notified_at) >= RENOTIFY_COOLDOWN


async def send_app_update_notifications(db):
    """
    Notify devices running an outdated app version that an update is
    available. Sends both a push notification and an in-app notification
    record (per spec, app_update_available needs both).

    Runs daily. See module docstring for the dedup/cooldown policy.
    """
    logger.info("Starting job: send_app_update_notifications")

    try:
        from services.models import DeviceToken
        from services.firebase_notification_service import FirebaseNotificationService
        from services.notification_service import NotificationService

        with db.get_session() as session:
            now = datetime.now(timezone.utc)

            latest_versions = {
                "ios": _get_latest_version(session, "ios"),
                "android": _get_latest_version(session, "android"),
            }

            notified_count = 0

            for platform in ("ios", "android"):
                latest_version = latest_versions[platform]
                if not latest_version:
                    logger.info(
                        f"send_app_update_notifications: no latest_{platform}_version "
                        f"configured in AppSetting, skipping {platform} devices"
                    )
                    continue

                candidate_devices = (
                    session.query(DeviceToken)
                    .filter(
                        DeviceToken.is_active == True,
                        DeviceToken.device_type == platform,
                        DeviceToken.app_version.isnot(None),
                    )
                    .all()
                )

                for device in candidate_devices:
                    try:
                        if not _is_outdated(device.app_version, latest_version):
                            continue

                        if not _should_notify(device, now):
                            continue

                        push_result = await FirebaseNotificationService().send_app_update_notification(
                            device_tokens=[device.device_token],
                            latest_version=latest_version,
                        )
                        if push_result.get("failure"):
                            logger.error(
                                f"send_app_update_notifications: push failed for "
                                f"device {device.id}: {push_result}"
                            )

                        NotificationService(session).create(
                            recipient_id=device.user_id,
                            title="Update Available",
                            body=(
                                f"Version {latest_version} is here. Update "
                                f"LawNeeds for the latest features and fixes."
                            ),
                            notification_type="app_update_available",
                            data={
                                "screen": "AppUpdate",
                                "action": "open_store",
                                "latest_version": latest_version,
                            },
                        )

                        device.app_update_notified_at = now
                        notified_count += 1

                    except Exception as e:
                        logger.error(
                            f"Error notifying device {device.id} of app update: {str(e)}"
                        )
                        continue

            logger.info(
                f"send_app_update_notifications: notified {notified_count} device(s)"
            )

        logger.info("✅ Completed job: send_app_update_notifications")

    except Exception as e:
        logger.error(f"❌ Error in send_app_update_notifications: {str(e)}")