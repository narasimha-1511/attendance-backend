
from django.conf import settings
from .notifier.firebase_notifier import Notifier

if settings.FIREBASE_SERVICE_ACCOUNT_CREDENTIAL:
    notifier = Notifier(settings.FIREBASE_SERVICE_ACCOUNT_CREDENTIAL)
else:
    notifier = None 

def pushNotification(fcmtokens, title, description, tag='attendance_reminder'):
    notifier.notify(title, description, fcmtokens, tag)
