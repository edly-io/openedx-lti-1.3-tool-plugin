from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('openedx_lti_tool_plugin', '0010_add_lti_activity_lineitem'),
    ]

    operations = [
        migrations.AddField(
            model_name='ltigradedresource',
            name='last_score_given',
            field=models.FloatField(blank=True, null=True, help_text='Last score value successfully sent to the platform (used to skip redundant publishes).'),
        ),
        migrations.AddField(
            model_name='ltigradedresource',
            name='last_score_maximum',
            field=models.FloatField(blank=True, null=True, help_text='Score maximum of the last score successfully sent to the platform.'),
        ),
    ]
