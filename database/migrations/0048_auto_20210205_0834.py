# Generated by Django 2.2.14 on 2021-02-05 08:34

import database.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('database', '0047_merge_20210204_1739'),
    ]

    operations = [
        migrations.AlterField(
            model_name='foresttracker',
            name='end_time',
            field=models.TimeField(null=True),
        ),
        migrations.AlterField(
            model_name='foresttracker',
            name='forest_tree',
            field=models.TextField(),
        ),
        migrations.AlterField(
            model_name='foresttracker',
            name='stacktrace',
            field=models.TextField(blank=True, default=None, null=True),
        ),
        migrations.AlterField(
            model_name='foresttracker',
            name='start_time',
            field=models.TimeField(null=True),
        ),
        migrations.AlterField(
            model_name='foresttracker',
            name='status',
            field=models.IntegerField(choices=[(1, 'queued'), (2, 'running'), (3, 'success'), (4, 'error')]),
        ),
        migrations.AlterField(
            model_name='study',
            name='object_id',
            field=models.CharField(help_text='ID used for naming S3 files', max_length=24, unique=True, validators=[database.validators.LengthValidator(24)]),
        ),
    ]
