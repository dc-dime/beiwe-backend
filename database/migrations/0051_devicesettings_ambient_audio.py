# Generated by Django 2.2.14 on 2021-04-06 03:43

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('database', '0050_participant_unregistered'),
    ]

    operations = [
        migrations.AddField(
            model_name='devicesettings',
            name='ambient_audio',
            field=models.BooleanField(default=False),
        ),
    ]
