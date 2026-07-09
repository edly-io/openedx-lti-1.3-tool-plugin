from django.db import migrations, models


def clear_stale_lineitems(apps, schema_editor):
    """Delete pre-existing rows before applying the new unique constraint.

    Rows created before ``resource_link_id`` existed get it backfilled to '', so multiple
    rows for the same (platform, problem) across different contexts now collide under the
    new (platform_id, resource_link_id, problem_id) key. These rows are a regenerable
    cache (recreated by setup_problem_lineitems on the next per-problem launch), so it is
    safe to clear them here.
    """
    LtiActivityLineitem = apps.get_model('openedx_lti_tool_plugin', 'LtiActivityLineitem')
    LtiActivityLineitem.objects.all().delete()


class Migration(migrations.Migration):

    dependencies = [
        ('openedx_lti_tool_plugin', '0013_ltitoolconfiguration_grade_passback_mode'),
    ]

    operations = [
        migrations.AddField(
            model_name='ltiactivitylineitem',
            name='resource_link_id',
            field=models.CharField(
                blank=True,
                default='',
                help_text='LTI resource link id — the specific platform activity/placement.',
                max_length=255,
            ),
        ),
        migrations.RunPython(clear_stale_lineitems, migrations.RunPython.noop),
        migrations.AlterUniqueTogether(
            name='ltiactivitylineitem',
            unique_together={('platform_id', 'resource_link_id', 'problem_id')},
        ),
    ]
