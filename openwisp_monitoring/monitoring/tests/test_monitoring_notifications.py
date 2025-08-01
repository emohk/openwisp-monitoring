from datetime import timedelta
from unittest.mock import patch

from django.utils import timezone
from freezegun import freeze_time
from swapper import load_model

from ...device.tests import (
    DeviceMonitoringTestCase,
    DeviceMonitoringTransactionTestcase,
)
from .. import settings as app_settings

Metric = load_model("monitoring", "Metric")
AlertSettings = load_model("monitoring", "AlertSettings")
Notification = load_model("openwisp_notifications", "Notification")
Device = load_model("config", "Device")
Config = load_model("config", "Config")
OrganizationUser = load_model("openwisp_users", "OrganizationUser")

notification_queryset = Notification.objects.order_by("timestamp")
start_time = timezone.now()
ten_minutes_ago = start_time - timedelta(minutes=10)
ten_minutes_after = start_time + timedelta(minutes=10)


class TestMonitoringNotifications(DeviceMonitoringTestCase):
    device_model = Device
    config_model = Config

    def test_general_check_threshold_crossed_immediate(self):
        admin = self._create_admin()
        m = self._create_general_metric(name="load")
        self._create_alert_settings(
            metric=m, custom_operator=">", custom_threshold=90, custom_tolerance=0
        )

        with self.subTest("Test notification for metric exceeding alert settings"):
            m.write(99)
            m.refresh_from_db()
            self.assertEqual(m.is_healthy, False)
            self.assertEqual(m.is_healthy_tolerant, False)
            self.assertEqual(Notification.objects.count(), 1)
            n = notification_queryset.first()
            self.assertEqual(n.recipient, admin)
            self.assertEqual(n.actor, m)
            self.assertEqual(n.action_object, m.alertsettings)
            self.assertEqual(n.level, "warning")

        with self.subTest("Test no double alarm for metric exceeding alert settings"):
            m.write(95)
            m.refresh_from_db()
            self.assertEqual(m.is_healthy, False)
            self.assertEqual(m.is_healthy_tolerant, False)
            self.assertEqual(Notification.objects.count(), 1)

        with self.subTest("Test notification for metric falling behind alert settings"):
            m.write(60)
            m.refresh_from_db()
            self.assertEqual(m.is_healthy, True)
            self.assertEqual(m.is_healthy_tolerant, True)
            self.assertEqual(Notification.objects.count(), 2)
            n = notification_queryset.last()
            self.assertEqual(n.recipient, admin)
            self.assertEqual(n.actor, m)
            self.assertEqual(n.action_object, m.alertsettings)
            self.assertEqual(n.level, "info")

        with self.subTest(
            "Test no double alarm for metric falling behind alert settings"
        ):
            m.write(40)
            m.refresh_from_db()
            self.assertEqual(m.is_healthy, True)
            self.assertEqual(m.is_healthy_tolerant, True)
            self.assertEqual(Notification.objects.count(), 2)

    @patch.object(app_settings, "TOLERANCE_INTERVAL", 60)
    def test_general_check_threshold_crossed_deferred(self):
        self._create_admin()
        m = self._create_general_metric(name="load")
        self._create_alert_settings(
            metric=m, custom_operator=">", custom_threshold=90, custom_tolerance=1
        )
        with freeze_time(start_time):
            m.write(99)
        with freeze_time(ten_minutes_after):
            m.write(99)
        m.refresh_from_db()
        self.assertEqual(m.is_healthy, False)
        self.assertEqual(m.is_healthy_tolerant, True)
        self.assertEqual(Notification.objects.count(), 0)

    @patch.object(app_settings, "TOLERANCE_INTERVAL", 60)
    def test_general_check_threshold_deferred_not_crossed(self):
        self._create_admin()
        m = self._create_general_metric(name="load")
        self._create_alert_settings(
            metric=m, custom_operator=">", custom_threshold=90, custom_tolerance=1
        )
        m.write(99)
        m.refresh_from_db(fields=["is_healthy", "is_healthy_tolerant"])
        self.assertEqual(m.is_healthy, False)
        self.assertEqual(m.is_healthy_tolerant, True)
        self.assertEqual(Notification.objects.count(), 0)

    def test_resources_metric_threshold_deferred_not_crossed(self):
        # Verify that newly created metrics within threshold limit don't send any notifications
        self._create_admin()
        self.create_test_data()
        self.assertEqual(Notification.objects.count(), 0)

    @patch.object(app_settings, "TOLERANCE_INTERVAL", 300)
    def test_general_check_threshold_crossed_for_long_time(self):
        """Test threshold remains crossed for a long time.

        This is the most common scenario: incoming metrics will always be
        stored with the current timestamp, which means the system must be
        able to look back in previous measurements to see if the
        AlertSettings has been crossed for long enough.
        """
        admin = self._create_admin()
        m = self._create_general_metric(name="load")
        self._create_alert_settings(
            metric=m, custom_operator=">", custom_threshold=90, custom_tolerance=5
        )

        with self.subTest("Test no notification is generated for healthy status"):
            with freeze_time(ten_minutes_ago):
                m.write(89)
            m.refresh_from_db()
            self.assertEqual(m.is_healthy, True)
            self.assertEqual(m.is_healthy_tolerant, True)
            self.assertEqual(Notification.objects.count(), 0)

        # When using UDP for writing data to the timeseries, reading the
        # metric from the database provides a time delay that allows the
        # timeseries database to process the transaction.
        self._read_metric(m)
        with self.subTest("Test no notification is generated when check=False"):
            with freeze_time(ten_minutes_ago):
                m.write(91, check=False)
            self.assertEqual(Notification.objects.count(), 0)

        self._read_metric(m)
        with self.subTest("Test notification for metric with current timestamp"):
            # There is only a single offending point in the tolerance search window,
            # thus notification is not sent
            with freeze_time(start_time):
                m.write(92)
            m.refresh_from_db()
            self.assertEqual(m.is_healthy, False)
            self.assertEqual(m.is_healthy_tolerant, True)
            self.assertEqual(Notification.objects.count(), 0)

            # There are two offending points in the tolerance search window,
            # thus notification is sent
            with freeze_time(start_time + timedelta(minutes=5)):
                m.write(95)
            m.refresh_from_db()
            self.assertEqual(m.is_healthy, False)
            self.assertEqual(m.is_healthy_tolerant, False)
            self.assertEqual(Notification.objects.count(), 1)
            n = notification_queryset.first()
            self.assertEqual(n.recipient, admin)
            self.assertEqual(n.actor, m)
            self.assertEqual(n.action_object, m.alertsettings)
            self.assertEqual(n.level, "warning")

        self._read_metric(m)
        with self.subTest("Test no recovery notification yet (tolerance not passed)"):
            with freeze_time(start_time + timedelta(minutes=6)):
                m.write(50)
            m.refresh_from_db()
            self.assertEqual(m.is_healthy, True)
            self.assertEqual(m.is_healthy_tolerant, False)
            self.assertEqual(Notification.objects.count(), 1)

        self._read_metric(m)
        with self.subTest("Tolerance still not passed, not expecting a recovery yet"):
            with freeze_time(start_time + timedelta(minutes=7)):
                m.write(51)
            m.refresh_from_db()
            self.assertEqual(m.is_healthy, True)
            self.assertEqual(m.is_healthy_tolerant, False)
            self.assertEqual(Notification.objects.count(), 1)

        self._read_metric(m)
        with self.subTest("Test recovery notification after tolerance is passed"):
            with freeze_time(start_time + timedelta(minutes=12)):
                m.write(50)
            m.refresh_from_db()
            self.assertEqual(m.is_healthy, True)
            self.assertEqual(m.is_healthy_tolerant, True)
            self.assertEqual(Notification.objects.count(), 2)
            n = notification_queryset.last()
            self.assertEqual(n.recipient, admin)
            self.assertEqual(n.actor, m)
            self.assertEqual(n.action_object, m.alertsettings)
            self.assertEqual(n.level, "info")

    def test_object_check_threshold_crossed_immediate(self):
        admin = self._create_admin()
        om = self._create_object_metric(name="load")
        alert_s = self._create_alert_settings(
            metric=om, custom_operator=">", custom_threshold=90, custom_tolerance=0
        )

        with self.subTest(
            "Test notification for object metric exceeding alert settings"
        ):
            om.write(99)
            om.refresh_from_db()
            self.assertEqual(om.is_healthy, False)
            self.assertEqual(om.is_healthy_tolerant, False)
            self.assertEqual(Notification.objects.count(), 1)
            n = notification_queryset.first()
            self.assertEqual(n.recipient, admin)
            self.assertEqual(n.actor, om)
            self.assertEqual(n.action_object, alert_s)
            self.assertEqual(n.target, om.content_object)
            self.assertEqual(n.level, "warning")

        with self.subTest(
            "Test no double alarm for object metric exceeding alert settings"
        ):
            om.write(95)
            om.refresh_from_db()
            self.assertEqual(om.is_healthy, False)
            self.assertEqual(om.is_healthy_tolerant, False)
            self.assertEqual(Notification.objects.count(), 1)

        with self.subTest(
            "Test notification for object metric falling behind alert settings"
        ):
            om.write(60)
            om.refresh_from_db()
            self.assertEqual(om.is_healthy, True)
            self.assertEqual(om.is_healthy_tolerant, True)
            self.assertEqual(Notification.objects.count(), 2)
            n = notification_queryset.last()
            self.assertEqual(n.recipient, admin)
            self.assertEqual(n.actor, om)
            self.assertEqual(n.action_object, alert_s)
            self.assertEqual(n.target, om.content_object)
            self.assertEqual(n.level, "info")

        with self.subTest(
            "Test no double alarm for object metric falling behind alert settings"
        ):
            om.write(40)
            om.refresh_from_db()
            self.assertEqual(om.is_healthy, True)
            self.assertEqual(om.is_healthy_tolerant, True)
            self.assertEqual(Notification.objects.count(), 2)

    @patch.object(app_settings, "TOLERANCE_INTERVAL", 60)
    def test_object_check_threshold_crossed_deferred(self):
        admin = self._create_admin()
        om = self._create_object_metric(name="load")
        alert_s = self._create_alert_settings(
            metric=om, custom_operator=">", custom_threshold=90, custom_tolerance=1
        )
        with freeze_time(start_time):
            om.write(99)
        # Metric points are outside the tolerance interval,
        # thus tolerance is not passed yet.
        with freeze_time(ten_minutes_after):
            om.write(99)
        om.refresh_from_db()
        self.assertEqual(om.is_healthy, False)
        self.assertEqual(om.is_healthy_tolerant, True)
        with freeze_time(ten_minutes_after + timedelta(minutes=1)):
            om.write(99)
        om.refresh_from_db()
        self.assertEqual(om.is_healthy, False)
        self.assertEqual(om.is_healthy_tolerant, False)
        self.assertEqual(Notification.objects.count(), 1)
        n = notification_queryset.first()
        self.assertEqual(n.recipient, admin)
        self.assertEqual(n.actor, om)
        self.assertEqual(n.action_object, alert_s)
        self.assertEqual(n.target, om.content_object)
        self.assertEqual(n.level, "warning")

    @patch.object(app_settings, "TOLERANCE_INTERVAL", 60)
    def test_object_check_threshold_deferred_not_crossed(self):
        self._create_admin()
        om = self._create_object_metric(name="load")
        self._create_alert_settings(
            metric=om, custom_operator=">", custom_threshold=90, custom_tolerance=1
        )
        om.write(99)
        om.refresh_from_db()
        self.assertEqual(om.is_healthy, False)
        self.assertEqual(om.is_healthy_tolerant, True)
        self.assertEqual(Notification.objects.count(), 0)

    @patch.object(app_settings, "TOLERANCE_INTERVAL", 60)
    def test_object_check_threshold_crossed_for_long_time(self):
        admin = self._create_admin()
        om = self._create_object_metric(name="load")
        alert_s = self._create_alert_settings(
            metric=om, custom_operator=">", custom_threshold=90, custom_tolerance=1
        )
        with freeze_time(ten_minutes_ago):
            self._write_metric(om, 89)
        self.assertEqual(Notification.objects.count(), 0)
        with freeze_time(ten_minutes_ago):
            self._write_metric(om, 91, check=False)
        self.assertEqual(Notification.objects.count(), 0)
        # The metric has not crossed threshold within tolerance interval
        with freeze_time(start_time):
            self._write_metric(om, 92)
        om.refresh_from_db()
        self.assertEqual(om.is_healthy, False)
        self.assertEqual(om.is_healthy_tolerant, True)
        self.assertEqual(Notification.objects.count(), 0)
        # The metric has crossed threshold within tolerance interval,
        with freeze_time(start_time + timedelta(minutes=1)):
            self._write_metric(om, 95)
        om.refresh_from_db()
        self.assertEqual(om.is_healthy, False)
        self.assertEqual(om.is_healthy_tolerant, False)
        self.assertEqual(Notification.objects.count(), 1)
        n = notification_queryset.first()
        self.assertEqual(n.recipient, admin)
        self.assertEqual(n.actor, om)
        self.assertEqual(n.action_object, alert_s)
        self.assertEqual(n.target, om.content_object)
        self.assertEqual(n.level, "warning")
        # ensure double alarm not sent
        with freeze_time(start_time + timedelta(minutes=2)):
            self._write_metric(om, 98)
        om.refresh_from_db()
        self.assertEqual(om.is_healthy, False)
        self.assertEqual(om.is_healthy_tolerant, False)
        self.assertEqual(Notification.objects.count(), 1)
        # value back to normal but tolerance not passed yet
        with freeze_time(start_time + timedelta(minutes=2.5)):
            self._write_metric(om, 60)
        om.refresh_from_db()
        self.assertEqual(om.is_healthy, True)
        self.assertEqual(om.is_healthy_tolerant, False)
        self.assertEqual(Notification.objects.count(), 1)
        # tolerance passed
        with freeze_time(start_time + timedelta(minutes=3.5)):
            self._write_metric(om, 60)
        om.refresh_from_db()
        self.assertEqual(om.is_healthy, True)
        self.assertEqual(om.is_healthy_tolerant, True)
        self.assertEqual(Notification.objects.count(), 2)
        n = notification_queryset.last()
        self.assertEqual(n.recipient, admin)
        self.assertEqual(n.actor, om)
        self.assertEqual(n.action_object, alert_s)
        self.assertEqual(n.target, om.content_object)
        self.assertEqual(n.level, "info")
        # ensure double alarm not sent
        with freeze_time(ten_minutes_after + timedelta(minutes=5)):
            self._write_metric(om, 40)
        om.refresh_from_db()
        self.assertEqual(om.is_healthy, True)
        self.assertEqual(om.is_healthy_tolerant, True)
        self.assertEqual(Notification.objects.count(), 2)

    @freeze_time(start_time)
    @patch.object(app_settings, "TOLERANCE_INTERVAL", 60)
    def test_object_check_threshold_crossed_historical_data(self):
        """
        Do not evaluate threshold crossed for historical data
        """
        self._create_admin()
        om = self._create_object_metric(name="load")
        self._create_alert_settings(
            metric=om, custom_operator=">", custom_threshold=90, custom_tolerance=1
        )

        # We need to write with "time" argument instead of freeze_time to
        # simulate historical data
        self._write_metric(om, 99, time=start_time - timedelta(minutes=60))
        om.refresh_from_db()
        self.assertEqual(om.is_healthy, True)
        self.assertEqual(om.is_healthy_tolerant, True)
        self.assertEqual(Notification.objects.count(), 0)

        self._write_metric(om, 99, time=start_time - timedelta(minutes=10))
        om.refresh_from_db()
        self.assertEqual(om.is_healthy, True)
        self.assertEqual(om.is_healthy_tolerant, True)
        self.assertEqual(Notification.objects.count(), 0)

        # Writing real-time data should enforce the threshold check
        self._write_metric(om, 99, time=start_time)
        om.refresh_from_db()
        self.assertEqual(om.is_healthy, False)
        self.assertEqual(om.is_healthy_tolerant, True)
        self.assertEqual(Notification.objects.count(), 0)

        self._write_metric(om, 99, time=start_time + timedelta(minutes=1))
        om.refresh_from_db()
        self.assertEqual(om.is_healthy, False)
        self.assertEqual(om.is_healthy_tolerant, False)
        self.assertEqual(Notification.objects.count(), 1)

    @patch.object(app_settings, "TOLERANCE_INTERVAL", 600)
    def test_flapping_metric_with_tolerance(self):
        self._create_admin()
        om = self._create_object_metric(name="ping")
        self._create_alert_settings(
            metric=om, custom_operator="<", custom_threshold=1, custom_tolerance=10
        )
        with freeze_time(start_time - timedelta(minutes=25)):
            om.write(1)
            self.assertEqual(Notification.objects.count(), 0)
            om.refresh_from_db()
            self.assertEqual(om.is_healthy, True)
            self.assertEqual(om.is_healthy_tolerant, True)
        with freeze_time(start_time - timedelta(minutes=20)):
            om.write(0)
            self.assertEqual(Notification.objects.count(), 0)
            om.refresh_from_db()
            self.assertEqual(om.is_healthy, False)
            self.assertEqual(om.is_healthy_tolerant, True)
        with freeze_time(start_time - timedelta(minutes=15)):
            om.write(1)
            self.assertEqual(Notification.objects.count(), 0)
            om.refresh_from_db()
            self.assertEqual(om.is_healthy, True)
            self.assertEqual(om.is_healthy_tolerant, True)
        with freeze_time(start_time - timedelta(minutes=10)):
            om.write(0)
            self.assertEqual(Notification.objects.count(), 0)
            om.refresh_from_db()
            self.assertEqual(om.is_healthy, False)
            self.assertEqual(om.is_healthy_tolerant, True)
        with freeze_time(start_time - timedelta(minutes=5)):
            om.write(0)
            self.assertEqual(Notification.objects.count(), 0)
            om.refresh_from_db()
            self.assertEqual(om.is_healthy, False)
            self.assertEqual(om.is_healthy_tolerant, True)
        with freeze_time(start_time):
            om.write(0)
            self.assertEqual(Notification.objects.count(), 1)
            om.refresh_from_db()
            self.assertEqual(om.is_healthy, False)
            self.assertEqual(om.is_healthy_tolerant, False)

    def test_notification_types(self):
        self._create_admin()
        m = self._create_object_metric(name="load")
        self._create_alert_settings(
            metric=m, custom_operator=">", custom_threshold=90, custom_tolerance=0
        )
        exp_message = (
            "{n.actor.name} for device "
            '<a href="https://example.com/admin/openwisp_users/user/{n.target.id}/change/">tester</a>'
            " {n.verb}."
        )
        with self.subTest("Test notification for 'alert settings crossed'"):
            m.write(99)
            n = notification_queryset.first()
            self.assertEqual(n.level, "warning")
            self.assertEqual(n.verb, "crossed the threshold")
            self.assertEqual(
                n.email_subject, f"[example.com] PROBLEM: {n.actor.name} {n.target}"
            )
            self.assertIn(exp_message.format(n=n), n.message)

        with self.subTest("Test notification for 'under alert settings'"):
            m.write(80)
            n = notification_queryset.last()
            self.assertEqual(n.level, "info")
            self.assertEqual(n.verb, "returned within the threshold")
            self.assertEqual(
                n.email_subject, f"[example.com] RECOVERY: {n.actor.name} {n.target}"
            )
            self.assertIn(exp_message.format(n=n), n.message)

    def test_alerts_disabled(self):
        self._create_admin()
        d = self._create_device(organization=self._get_org())
        m = self._create_general_metric(name="load", content_object=d)
        self._create_alert_settings(
            metric=m,
            custom_operator=">",
            custom_threshold=90,
            custom_tolerance=1,
            is_active=False,
        )
        with freeze_time(start_time):
            m.write(99)
        with freeze_time(ten_minutes_after):
            m.write(99)
        m.refresh_from_db()
        self.assertEqual(m.is_healthy, False)
        self.assertEqual(m.is_healthy_tolerant, False)
        d.refresh_from_db()
        self.assertEqual(d.monitoring.status, "problem")
        self.assertEqual(Notification.objects.count(), 0)

    def test_alert_field(self):
        admin = self._create_admin()

        def _create_alert_field_test_env():
            m = self._create_general_metric(configuration="test_alert_field")
            self._create_alert_settings(
                metric=m, custom_operator=">", custom_threshold=30, custom_tolerance=0
            )
            return m

        with self.subTest("Test notification for metric without related field"):
            m = _create_alert_field_test_env()
            with self.assertRaises(ValueError) as err:
                m.write(1)
            self.assertEqual(
                str(err.exception),
                'write() missing keyword argument: "extra_values" required for alert on related field',
            )
            m.refresh_from_db()
            self.assertEqual(m.is_healthy, True)
            self.assertEqual(m.is_healthy_tolerant, True)
            self.assertEqual(Notification.objects.count(), 0)

        with self.subTest("Test notification for metric on different related field"):
            m = _create_alert_field_test_env()
            with self.assertRaises(ValueError) as err:
                m.write(10, extra_values={"test_related_3": 40})
            self.assertEqual(
                str(err.exception),
                '"test_related_3" is not defined for alert_field in metric configuration',
            )
            m.refresh_from_db()
            self.assertEqual(m.is_healthy, True)
            self.assertEqual(m.is_healthy_tolerant, True)
            self.assertEqual(Notification.objects.count(), 0)

        with self.subTest("Test notification for metric with multiple related fields"):
            m = _create_alert_field_test_env()
            m.write(10, extra_values={"test_related_2": 40, "test_related_3": 20})
            m.refresh_from_db()
            self.assertEqual(m.is_healthy, False)
            self.assertEqual(m.is_healthy_tolerant, False)
            self.assertEqual(Notification.objects.count(), 1)
            n = notification_queryset.first()
            self.assertEqual(n.recipient, admin)
            self.assertEqual(n.actor, m)
            self.assertEqual(n.action_object, m.alertsettings)
            self.assertEqual(n.level, "warning")
        Notification.objects.all().delete()

        with self.subTest(
            "Test notification for metric exceeding related field alert settings"
        ):
            m = _create_alert_field_test_env()
            m.write(10, extra_values={"test_related_2": 40})
            m.refresh_from_db()
            self.assertEqual(m.is_healthy, False)
            self.assertEqual(m.is_healthy_tolerant, False)
            self.assertEqual(Notification.objects.count(), 1)
            n = notification_queryset.first()
            self.assertEqual(n.recipient, admin)
            self.assertEqual(n.actor, m)
            self.assertEqual(n.action_object, m.alertsettings)
            self.assertEqual(n.level, "warning")
        Notification.objects.all().delete()

        with self.subTest(
            "Test notification for metric falling behind related field alert settings"
        ):
            m = _create_alert_field_test_env()
            m.write(30, extra_values={"test_related_2": 25})
            m.refresh_from_db()
            self.assertEqual(m.is_healthy, True)
            self.assertEqual(m.is_healthy_tolerant, True)
            self.assertEqual(Notification.objects.count(), 0)

        with self.subTest(
            "Test no double alarm for metric exceeding related field alert settings"
        ):
            m = _create_alert_field_test_env()
            m.write(20, extra_values={"test_related_2": 35})
            m.refresh_from_db()
            self.assertEqual(m.is_healthy, False)
            self.assertEqual(m.is_healthy_tolerant, False)
            self.assertEqual(Notification.objects.count(), 1)
            n = notification_queryset.first()
            Notification.objects.all().delete()
            # check for double alarm
            m.write(20, extra_values={"test_related_2": 40})
            m.refresh_from_db()
            self.assertEqual(m.is_healthy, False)
            self.assertEqual(m.is_healthy_tolerant, False)
            self.assertEqual(Notification.objects.count(), 0)

    @patch.object(app_settings, "TOLERANCE_INTERVAL", 60)
    def test_general_check_threshold_with_alert_field_crossed_deferred(self):
        admin = self._create_admin()
        m = self._create_general_metric(configuration="test_alert_field")
        self._create_alert_settings(
            metric=m, custom_operator=">", custom_threshold=30, custom_tolerance=1
        )
        with freeze_time(start_time):
            m.write(10, extra_values={"test_related_2": 35})
        m.refresh_from_db()
        self.assertEqual(m.is_healthy, False)
        self.assertEqual(m.is_healthy_tolerant, True)
        # Metric points are outside the tolerance interval,
        # thus tolerance is not passed yet.
        with freeze_time(ten_minutes_after):
            m.write(10, extra_values={"test_related_2": 35})
        m.refresh_from_db()
        self.assertEqual(m.is_healthy, False)
        self.assertEqual(m.is_healthy_tolerant, True)
        with freeze_time(ten_minutes_after + timedelta(minutes=1)):
            m.write(10, extra_values={"test_related_2": 35})
        m.refresh_from_db()
        self.assertEqual(m.is_healthy, False)
        self.assertEqual(m.is_healthy_tolerant, False)
        self.assertEqual(Notification.objects.count(), 1)
        n = notification_queryset.first()
        self.assertEqual(n.recipient, admin)
        self.assertEqual(n.actor, m)
        self.assertEqual(n.action_object, m.alertsettings)
        self.assertEqual(n.level, "warning")

    @patch.object(app_settings, "TOLERANCE_INTERVAL", 60)
    def test_general_check_threshold_with_alert_field_deferred_not_crossed(self):
        self._create_admin()
        m = self._create_general_metric(configuration="test_alert_field")
        self._create_alert_settings(
            metric=m, custom_operator=">", custom_threshold=30, custom_tolerance=1
        )
        m.write(10, extra_values={"test_related_2": 32})
        m.refresh_from_db(fields=["is_healthy", "is_healthy_tolerant"])
        self.assertEqual(m.is_healthy, False)
        self.assertEqual(m.is_healthy_tolerant, True)
        self.assertEqual(Notification.objects.count(), 0)
        m.write(20, extra_values={"test_related_2": 35})
        m.refresh_from_db(fields=["is_healthy", "is_healthy_tolerant"])
        self.assertEqual(m.is_healthy, False)
        self.assertEqual(m.is_healthy_tolerant, True)
        self.assertEqual(Notification.objects.count(), 0)


class TestTransactionMonitoringNotifications(DeviceMonitoringTransactionTestcase):
    device_model = Device
    config_model = Config

    def _check_notification_parameters(self, notification, recepient, metric, target):
        self.assertEqual(notification.recipient, recepient)
        self.assertEqual(notification.actor, metric)
        self.assertEqual(notification.target, target)
        self.assertEqual(notification.action_object, metric.alertsettings)
        self.assertEqual(notification.level, "warning")
        self.assertEqual(notification.verb, "is not reachable")

    def test_cpu_metric_threshold_crossed(self):
        admin = self._create_admin()
        org = self._create_org()
        device = self._create_device(organization=org)
        # creates metric and alert settings
        data = self._data()
        data["resources"]["load"] = [0.99, 0.99, 0.99]
        response = self._post_data(device.id, device.key, data)
        self.assertEqual(response.status_code, 200)
        # retrieve created metric
        metric = Metric.objects.get(name="CPU usage")
        # simplify test by setting tolerance to 0
        metric.alertsettings.custom_tolerance = 0
        metric.alertsettings.save()
        # trigger alert
        metric.write(99.0)
        self.assertEqual(Notification.objects.count(), 1)
        n = Notification.objects.first()
        self.assertEqual(n.recipient, admin)
        self.assertEqual(n.actor, metric)
        self.assertEqual(n.action_object, metric.alertsettings)
        self.assertEqual(n.level, "warning")

    @patch.object(app_settings, "TOLERANCE_INTERVAL", 60)
    def test_multiple_notifications(self):
        testorg = self._create_org()
        admin = self._create_admin()
        staff = self._create_user(
            username="staff", email="staff@staff.com", password="staff", is_staff=True
        )
        self._create_user(
            username="staff-lone",
            email="staff-lone@staff.com",
            password="staff",
            is_staff=True,
        )
        user = self._create_user(is_staff=False)
        OrganizationUser.objects.create(user=user, organization=testorg, is_admin=True)
        OrganizationUser.objects.create(user=staff, organization=testorg, is_admin=True)
        self.assertIsNotNone(staff.notificationsetting_set.filter(organization=testorg))

        with self.subTest("Test general metric multiple notifications"):
            m = self._create_general_metric(name="load")
            alert_s = self._create_alert_settings(
                metric=m, custom_operator=">", custom_threshold=90, custom_tolerance=1
            )
            m._notify_users(notification_type="ping_problem", alert_settings=alert_s)
            self.assertEqual(Notification.objects.count(), 1)
            n = notification_queryset.first()
            self._check_notification_parameters(n, admin, m, None)
            self.assertIn(
                'The device <a href="#">None</a> is not reachable.', n.message
            )
        Notification.objects.all().delete()

        with self.subTest("Test object metric multiple notifications"):
            d = self._create_device(organization=testorg)
            om = self._create_object_metric(name="load", content_object=d)
            alert_s = self._create_alert_settings(
                metric=om, custom_operator=">", custom_threshold=90, custom_tolerance=1
            )
            self.assertEqual(Notification.objects.count(), 0)
            om._notify_users(notification_type="ping_problem", alert_settings=alert_s)
            self.assertEqual(Notification.objects.count(), 2)
            n = notification_queryset.first()
            self._check_notification_parameters(n, admin, om, d)
            self.assertIn("is not reachable.", n.message)
            n = notification_queryset.last()
            self._check_notification_parameters(n, staff, om, d)
        Notification.objects.all().delete()

        with self.subTest("Test object metric multiple notifications no org"):
            om = self._create_object_metric(name="logins", content_object=user)
            alert_s = self._create_alert_settings(
                metric=om, custom_operator=">", custom_threshold=90, custom_tolerance=0
            )
            self.assertEqual(Notification.objects.count(), 0)
            om._notify_users(notification_type="ping_problem", alert_settings=alert_s)
            self.assertEqual(Notification.objects.count(), 1)
            n = notification_queryset.first()
            self._check_notification_parameters(n, admin, om, user)

    @patch.object(app_settings, "TOLERANCE_INTERVAL", 300)
    def test_passive_metric_alert(self):
        self._get_admin()
        data = {
            "type": "DeviceMonitoring",
            "resources": {
                "cpus": 1,
                "load": [0, 0, 0],
            },
        }
        device = self._create_device(organization=self._create_org())
        self._post_data(device.id, device.key, data)
        cpu_metric = Metric.objects.get(key="cpu")
        self.assertEqual(cpu_metric.is_healthy, True)
        self.assertEqual(cpu_metric.is_healthy_tolerant, True)
        self.assertEqual(Notification.objects.count(), 0)

        AlertSettings.objects.update(
            custom_tolerance=5,
            custom_threshold=90,
        )

        data["resources"]["load"] = [100, 100, 100]

        with freeze_time(timezone.now() + timedelta(minutes=1)):
            response = self._post_data(device.id, device.key, data)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(Notification.objects.count(), 0)
            cpu_metric.refresh_from_db()
            self.assertEqual(cpu_metric.is_healthy, False)
            self.assertEqual(cpu_metric.is_healthy_tolerant, True)

        with freeze_time(timezone.now() + timedelta(minutes=4)):
            response = self._post_data(device.id, device.key, data)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(Notification.objects.count(), 0)
            cpu_metric.refresh_from_db()
            self.assertEqual(cpu_metric.is_healthy, False)
            self.assertEqual(cpu_metric.is_healthy_tolerant, True)

        with freeze_time(timezone.now() + timedelta(minutes=9)):
            response = self._post_data(device.id, device.key, data)
            self.assertEqual(response.status_code, 200)
            cpu_metric.refresh_from_db()
            self.assertEqual(cpu_metric.is_healthy, False)
            self.assertEqual(cpu_metric.is_healthy_tolerant, False)
            self.assertEqual(Notification.objects.count(), 1)
