from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('openedx_lti_tool_plugin', '0011_ltigradedresource_last_score'),
    ]

    operations = [
        migrations.AlterField(
            model_name='ltiactivitylineitem',
            name='lineitem',
            field=models.URLField(
                blank=True,
                default='',
                help_text='Pre-created Moodle lineitem URL for this problem.',
                max_length=255,
            ),
        ),
    ]
