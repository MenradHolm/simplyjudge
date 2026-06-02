from django.apps import AppConfig


class JudgingAppConfig(AppConfig):
    name = 'judging_app'

    def ready(self):
        import judging_app.signals  # noqa: F401
