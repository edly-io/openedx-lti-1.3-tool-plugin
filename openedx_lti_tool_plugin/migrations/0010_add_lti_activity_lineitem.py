from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('openedx_lti_tool_plugin', '0009_update_lti_profile'),
    ]

    operations = [
        migrations.CreateModel(
            name='LtiActivityLineitem',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('resource_id', models.CharField(help_text='The launched course/content ID (e.g. course-v1:Org+Course+Run).', max_length=255)),
                ('problem_id', models.CharField(help_text='Usage key of the individual graded problem.', max_length=255)),
                ('lineitem', models.URLField(blank=True, default='', help_text='Pre-created Moodle lineitem URL for this problem.')),
                ('label', models.CharField(blank=True, default='', max_length=255)),
                ('lti_profile', models.ForeignKey(
                    help_text='The LTI profile associated with this activity.',
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='openedx_lti_tool_plugin_activity_lineitem',
                    to='openedx_lti_tool_plugin.ltiprofile',
                )),
            ],
            options={
                'verbose_name': 'LTI activity lineitem',
                'verbose_name_plural': 'LTI activity lineitems',
                'unique_together': {('lti_profile', 'resource_id', 'problem_id')},
            },
        ),
    ]
