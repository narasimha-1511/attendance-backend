from django.core.cache import cache
from django.db import models
from django.db.models import Count, Subquery, OuterRef
from django.db.models import F
from django.utils import timezone
from enum import Enum
from django.contrib.auth.models import User


def round_coordinates(num):
    return round(num, 10)


class AttendanceStatus(Enum):
    Present = 1
    Proxy = 2
    Absent = 3


# Create your models here.
class Student(models.Model):
    name = models.CharField(max_length=50)
    mail = models.EmailField(
        max_length=80, blank=False, null=False, unique=True, db_index=True
    )
    token = models.CharField(
        max_length=100, blank=True, null=True, unique=True, db_index=True
    )
    user = models.ForeignKey(User, default=None, null=True, blank=True, on_delete=models.DO_NOTHING)
    fcmtoken = models.CharField(max_length=255, blank=True, null=True)

    def get_id_number(self):
        if self.mail.endswith("@scaler.com"):
            return None
        return self.mail.split(".")[1].split("@")[0]

    def __str__(self):
        return self.mail

    @classmethod
    def get_object_with_token(cls, token):
        return Student.objects.get(token=token)

    @classmethod
    def get_all_students(cls):
        return Student.objects.filter(mail__endswith="@sst.scaler.com")

    def save(self, *args, **kwargs):
        self.mail = self.mail.lower()

        super().save(*args, **kwargs)

    def create_django_user(self):
        user = User.objects.create_user(
            username=self.mail, email=self.mail, password=None
        )
        user.set_unusable_password()
        user.save()
        return user

    def get_all_attendance(self, include_optional=False):
        if include_optional:
            return (
                ClassAttendance.objects.filter(student=self)
                .select_related("classattendancebybsm", "classattendancewithgeolocation")
            )
        else:
           return (
                ClassAttendance.objects.filter(student=self, subject__is_attendance_mandatory=True)
                .select_related("classattendancebybsm", "classattendancewithgeolocation")
            ) 
    
    @classmethod
    def get_aggregated_attendance(cls, attendances=None, student=None, include_optional=False):
        if student and not attendances:
            # If only the student is provided, fetch all attendances for the student
            attendances = student.get_all_attendance(include_optional)
        elif not include_optional:
           attendances = attendances.filter(subject__is_attendance_mandatory=True) 

        if not attendances:
            return {}

        if include_optional:
            subject_all_classe = SubjectClass.objects.all().annotate(
                course=F("subject__name")
            ).annotate(count=Count("id"))
        else:
            subject_all_classe = SubjectClass.objects.filter(is_attendance_mandatory=True).annotate(
                course=F("subject__name")
            ).values("subject__name").annotate(count=Count("id"))
        

        # Annotate each attendance with status_by_bsm and status_by_geo
        annotated_attendances = attendances.annotate(
            subject_name=F("subject__subject__name"),
            status_by_bsm=F("classattendancebybsm__status"),
            status_by_geo=F("classattendancewithgeolocation__status"),
        )

        # Use Case and When to count occurrences of each status combination
        aggregated_data = (
            annotated_attendances.values("subject_name", "status_by_bsm", "status_by_geo")
            .annotate(count=Count("id"))
        )

        # Create the final aggregated result
        result = {}
        uncategorised = "uncategorised"
        for item in aggregated_data:
            subject_name, status_by_geo, status_by_bsm = item["subject_name"], item["status_by_geo"], item["status_by_bsm"]
            if not subject_name:
                subject_name = uncategorised
            status_by_geo = ClassAttendanceWithGeoLocation.status_mapping.get(status_by_geo)
            status_by_bsm = ClassAttendanceByBSM.status_mapping.get(status_by_bsm)
            status = ClassAttendance.get_attendance_status_by_status(status_by_bsm, status_by_geo).name
            
            if subject_name not in result:
               result[subject_name] = {}
            
            if status not in result[subject_name]:
               result[subject_name][status] = 0 
            result[subject_name][status] += item["count"]
        
        for item in subject_all_classe:
            # print(item)
            name, count = item['subject__name'], item['count']
            if not name:
                name = uncategorised
            if name not in result:
               result[name] = {} 
            result[name]['totalClassCount'] = count

        return result

class Subject(models.Model):
    name = models.CharField(max_length=50)
    instructor_name = models.CharField(max_length=25)

    def __str__(self):
        return self.name


class SubjectClass(models.Model):
    name = models.CharField(max_length=50)
    attendance_start_time = models.DateTimeField()
    attendance_end_time = models.DateTimeField(blank=True, null=True)
    class_start_time = models.DateTimeField(db_index=True)
    class_end_time = models.DateTimeField()
    is_attendance_mandatory = models.BooleanField(default=True)
    subject = models.ForeignKey(
        Subject, default=None, null=True, blank=True, on_delete=models.CASCADE
    )
    is_attendance_by_geo_location_enabled = models.BooleanField(default=True)
    merge_attendace_with_class = models.ForeignKey('self', blank=True, null=True, on_delete=models.DO_NOTHING, default=None)

    def __str__(self):
        return (
            f"{self.class_start_time.astimezone().strftime('%d/%m/%Y')} => {self.name}"
        )
    
    def get_all_students_attendance_status(self):
        students_with_attendance = (
            Student.get_all_students()
            .annotate(
                attendance_id=Subquery(
                    ClassAttendance.objects.filter(
                        student=OuterRef('pk'),
                        subject=self
                    ).values('id')[:1]
                )
            )
        )

        result = []
        for student in students_with_attendance:
            attendance_id = student.attendance_id

            if attendance_id is not None:
                attendance = ClassAttendance(id=attendance_id).get_attendance_status()
            else:
                attendance = AttendanceStatus.Absent

            result.append((student, attendance))

        return result

    @classmethod
    def get_current_class(cls):
        cache_key = "get_current_class"
        result = cache.get(cache_key)

        if result is not None:
            return result

        current_time = timezone.now()
        filtered_subject_class = SubjectClass.objects.filter(
            class_start_time__lte=current_time, class_end_time__gte=current_time
        ).first()

        if filtered_subject_class:
            result = filtered_subject_class
        else:
            nearest_next_class = (
                SubjectClass.objects.filter(class_start_time__gt=current_time)
                .order_by("class_start_time")
                .first()
            )
            if nearest_next_class:
                result = nearest_next_class
            else:
                result = None

        cache.set(cache_key, result, 60 * 5)
        return result

    @classmethod
    def get_classes_for(cls, start=timezone.now().date(), next_x_days=1, use_cache=True):
        cache_key = f"get_todays_classs"

        if use_cache:
            result = cache.get(cache_key)
            if result is not None:
                return result
        
        end = start+timezone.timedelta(days=next_x_days-1)
        filtered_subject_class = SubjectClass.objects.filter(
            class_start_time__date__lte=start, class_end_time__date__gte=end
        )
        if use_cache:
            cache.set(cache_key, filtered_subject_class, 60 * 5)
        return filtered_subject_class

    def is_in_attendance_window(self):
        current_time = timezone.now()
        return (
            self.attendance_start_time
            <= current_time
            <= (
                self.attendance_end_time
                if self.attendance_end_time
                else self.class_end_time
            )
        )

    def get_all_attendance(self):
        all_students = (
            ClassAttendance.objects.filter(subject=self).select_related("student").all()
        )
        return all_students

    @classmethod
    def get_all_classes(cls):
        return cls.objects.all()


class ClassAttendance(models.Model):
    creation_time = models.DateTimeField(auto_now=True)
    student = models.ForeignKey(Student, on_delete=models.CASCADE, db_index=True)
    subject = models.ForeignKey(SubjectClass, on_delete=models.CASCADE, db_index=True)

    class Meta:
        # Make the combination of student and subject unique
        unique_together = ("student", "subject")

    def __str__(self):
        return self.student.mail + " " + self.subject.name

    @classmethod
    def get_attendance_status_for(cls, student, subject):
        obj = (
            ClassAttendance.objects.filter(student=student, subject=subject)
            .select_related("classattendancebybsm", "classattendancewithgeolocation")
        )
        if obj.exists():
            return obj.first().get_attendance_status()
        else:
            return AttendanceStatus.Absent

    @classmethod
    def all_subject_attendance(cls, student):
        from django.db.models import Min, OuterRef, Subquery

        min_creation_time_subquery = (
            ClassAttendance.objects.filter(student=student, subject=OuterRef("pk"))
            .values("subject")
            .annotate(min_creation_time=Min("creation_time"))
            .values("min_creation_time")
        )

        subject_classes_with_attendance = SubjectClass.objects.annotate(
            min_creation_time=Subquery(min_creation_time_subquery)
        ).values("name", "start_time", "end_time", "min_creation_time")

        return subject_classes_with_attendance

    # @classmethod
    # def get_all_student_attendance(cls, student):
    #     return ClassAttendance.objects.filter(student=student).values("subject").annotate(min_creation_time=Min('creation_time')).all()

    def get_attendance_by_bsm_status(self):
        if not hasattr(self, "classattendancebybsm"):
            return None
        return self.classattendancebybsm.get_attendance_status()

    def get_attendance_with_geo_location_status(self):
        if not hasattr(self, "classattendancewithgeolocation"):
            return None
        return self.classattendancewithgeolocation.get_attendance_status()

    def get_attendance_status(self):
        by_bsm = self.get_attendance_by_bsm_status()
        with_geo_location = self.get_attendance_with_geo_location_status()
        return self.get_attendance_status_by_status(by_bsm, with_geo_location)
    
    @classmethod
    def get_attendance_status_by_status(cls, status_by_bsm, status_by_geo):
        if status_by_bsm == AttendanceStatus.Present:
            return AttendanceStatus.Present

        if status_by_bsm == AttendanceStatus.Proxy:
            return AttendanceStatus.Proxy

        if status_by_geo == AttendanceStatus.Present:
            return AttendanceStatus.Present
        if status_by_geo == AttendanceStatus.Proxy:
            return AttendanceStatus.Proxy

        return AttendanceStatus.Absent


class ClassAttendanceWithGeoLocation(models.Model):
    STATUS_CHOICES = [
        ("proxy", "Proxy"),
        ("verified", "Verified"),
        ("standby", "Standby"),
        ("flaggers", "Flaggers"),
    ]
    status_mapping = {
        "verified": AttendanceStatus.Present,
        "standby": AttendanceStatus.Present,
        "proxy": AttendanceStatus.Proxy,
    }

    lat = models.DecimalField(max_digits=13, decimal_places=10)
    lon = models.DecimalField(max_digits=13, decimal_places=10)
    accuracy = models.DecimalField(max_digits=13, decimal_places=10)
    class_attendance = models.OneToOneField(ClassAttendance, on_delete=models.CASCADE)
    status = models.CharField(
        max_length=10,
        choices=STATUS_CHOICES,
        default="standby",  # Set the default value if needed
        db_index=True,
    )

    def get_attendance_status(self):
        return self.status_mapping.get(self.status)

    def save(self, *args, **kwargs):
        self.lat = round_coordinates(self.lat)
        self.lon = round_coordinates(self.lon)
        self.accuracy = round_coordinates(self.accuracy)

        super().save(*args, **kwargs)

    def __str__(self):
        return str(self.class_attendance)

    @classmethod
    def create_with(cls, student, subject, lat, lon, accuracy):
        class_attendance, is_created = ClassAttendance.objects.get_or_create(
            student=student, subject=subject
        )

        attendance, is_created = ClassAttendanceWithGeoLocation.objects.get_or_create(
            class_attendance=class_attendance,
           defaults={"lat": lat, "lon": lon, "accuracy": accuracy}, 
        )
        attendance.save()
        return class_attendance


class ClassAttendanceByBSM(models.Model):
    STATUS_CHOICES = [
        ("proxy", "Proxy"),
        ("present", "Present"),
        ("absent", "Absent"),
    ]
    status_mapping = {
        "present": AttendanceStatus.Present,
        "proxy": AttendanceStatus.Proxy,
        "absent": AttendanceStatus.Absent,
    }

    marked_by = models.ForeignKey(User, on_delete=models.CASCADE)
    class_attendance = models.OneToOneField(ClassAttendance, on_delete=models.CASCADE)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="present")

    def __str__(self):
        return (
            f"{self.class_attendance.student.name} {self.class_attendance.subject.name}"
        )

    def get_attendance_status(self):
        return self.status_mapping.get(self.status)

    @classmethod
    def create_with(cls, student, subject, status, marked_by):
        class_attendance, _ = ClassAttendance.objects.get_or_create(
            student=student, subject=subject
        )

        attendance, _ = ClassAttendanceByBSM.objects.update_or_create(
            class_attendance=class_attendance,
            defaults={"marked_by": marked_by, "status": status},
        )
        attendance.save()
        return attendance


class GeoLocationDataContrib(models.Model):
    label = models.SmallIntegerField(default=-2)
    student = models.ForeignKey(Student, on_delete=models.CASCADE)
    lat = models.DecimalField(max_digits=13, decimal_places=10)
    lon = models.DecimalField(max_digits=13, decimal_places=10)
    accuracy = models.DecimalField(max_digits=13, decimal_places=10)

    def save(self, *args, **kwargs):
        self.lat = round_coordinates(self.lat)
        self.lon = round_coordinates(self.lon)
        self.accuracy = round_coordinates(self.accuracy)

        super().save(*args, **kwargs)


class FalseAttemptGeoLocation(models.Model):
    student = models.ForeignKey(Student, on_delete=models.CASCADE, db_index=True)
    lat = models.DecimalField(max_digits=13, decimal_places=10)
    lon = models.DecimalField(max_digits=13, decimal_places=10)
    accuracy = models.DecimalField(max_digits=13, decimal_places=10)
    creation_time = models.DateTimeField(auto_now=True, blank=True, null=True)
    subject = models.ForeignKey(
        SubjectClass, on_delete=models.CASCADE, db_index=True, blank=True, null=True
    )

    def save(self, *args, **kwargs):
        self.lat = round_coordinates(self.lat)
        self.lon = round_coordinates(self.lon)
        self.accuracy = round_coordinates(self.accuracy)

        super().save(*args, **kwargs)

    def __str__(self):
        return str(self.student.mail)


class ProjectConfiguration(models.Model):
    APP_LATEST_VERSION = models.CharField(max_length=12)
    APK_FILE = models.TextField()

    def save(self, *args, **kwargs):
        # Override save to ensure only one instance is saved
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def get_config(cls):
        cache_key = "ProjectConfigurationSingeltonObject"
        result = cache.get(cache_key)

        if result is not None:
            return result
        # Load the singleton object and return its configuration values
        obj, _ = cls.objects.get_or_create(
            pk=1,
            defaults={
                "APP_LATEST_VERSION": "0.2.5",
                "APK_FILE": "https://drive.google.com/file/d/1dgL7fEq16OugBBxLo2Twn_SC6IGXYmjp/view?usp=sharing",
            },
        )
        cache.set(cache_key, obj, 60 * 5)
        return obj

