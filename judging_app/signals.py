from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver

from .models import EntryOrder
from .utils import send_automated_email


@receiver(pre_save, sender=EntryOrder)
def remember_entry_order_paid_state(sender, instance, **kwargs):
    if not instance.pk:
        instance._was_paid_before_save = False
        return

    instance._was_paid_before_save = (
        sender.objects.filter(pk=instance.pk, is_paid=True).exists()
    )


@receiver(post_save, sender=EntryOrder)
def send_entry_order_payment_receipt(sender, instance, created, **kwargs):
    was_paid_before_save = getattr(instance, '_was_paid_before_save', False)
    if not instance.is_paid or was_paid_before_save:
        return

    recipient_email = instance.user.email
    if not recipient_email:
        return

    send_automated_email(
        competition=instance.competition,
        subject=f"Payment receipt for {instance.competition.name}",
        template_name='emails/payment_receipt.txt',
        context={'order': instance, 'user': instance.user},
        recipient_list=[recipient_email],
    )
