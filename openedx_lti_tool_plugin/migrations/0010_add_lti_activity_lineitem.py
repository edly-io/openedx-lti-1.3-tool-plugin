from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('openedx_lti_tool_plugin', '0009_update_lti_profile'),
    ]

    operations = [
        migrations.CreateModel(
            name='LtiActivityLineitem',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('platform_id', models.CharField(help_text='LTI platform issuer (iss) — the Moodle server.', max_length=255)),
                ('context_id', models.CharField(help_text='LTI context claim id — the specific Moodle course.', max_length=255)),
                ('resource_id', models.CharField(help_text='The launched Open edX course/content ID.', max_length=255)),
                ('problem_id', models.CharField(help_text='Usage key of the individual graded problem.', max_length=255)),
                ('lineitem', models.URLField(blank=True, default='', help_text='Pre-created Moodle lineitem URL for this problem.')),
                ('label', models.CharField(blank=True, default='', max_length=255)),
            ],
            options={
                'verbose_name': 'LTI activity lineitem',
                'verbose_name_plural': 'LTI activity lineitems',
                'unique_together': {('platform_id', 'context_id', 'problem_id')},
            },
        ),
    ]
