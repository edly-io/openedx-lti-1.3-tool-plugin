import hashlib

from django.db import migrations, models

import openedx_lti_tool_plugin.resource_link_launch.ags.validators


def clear_stale_lineitems(apps, schema_editor):
    """Delete all LtiActivityLineitem rows before the new unique_key constraint is applied.

    Same precedent as migration 0014 (which did this for the same reason, on the same
    model, one column short of this one): LtiActivityLineitem is a regenerable cache
    (setup_problem_lineitems/relay_criterion_scores recreate a missing row on the next
    launch or score change), so clearing it is safe and far simpler than backfilling a hash
    for rows that are cheap to regenerate anyway.
    """
    LtiActivityLineitem = apps.get_model('openedx_lti_tool_plugin', 'LtiActivityLineitem')
    LtiActivityLineitem.objects.all().delete()


def backfill_graded_resource_unique_keys(apps, schema_editor):
    """Compute unique_key for every existing LtiGradedResource row.

    Unlike LtiActivityLineitem, these rows carry durable per-user state
    (last_score_given/last_score_maximum, used to skip redundant re-publishes) that would be
    lost by clearing them, and every field this hash needs (lti_profile_id, context_key,
    lineitem, criterion_key) already exists on every current row — criterion_key defaults to
    '' from earlier in this same migration — so backfilling here, instead of clearing, is
    both safer and straightforward. Mirrors LtiGradedResource.save()'s own hash computation
    exactly (same field order, same '\x1f' separator) so a row backfilled here computes the
    identical value the model would compute itself on its next save.
    """
    LtiGradedResource = apps.get_model('openedx_lti_tool_plugin', 'LtiGradedResource')
    for row in LtiGradedResource.objects.all():
        row.unique_key = hashlib.sha256(
            '\x1f'.join(
                str(part) for part in (row.lti_profile_id, row.context_key, row.lineitem, row.criterion_key)
            ).encode(),
        ).hexdigest()
        row.save(update_fields=['unique_key'])


class Migration(migrations.Migration):
    """Add per-criterion AGS relay support.

    `criterion_key` lets a single Open edX block (`problem_id`/`context_key`) map to several
    Moodle lineitems instead of one — the empty-string default preserves the existing
    single-lineitem-per-block rows and behavior exactly. `resource_link_id` and `lineitems_url`
    on `LtiGradedResource` capture launch-only data (the Moodle activity id and the AGS
    lineitems collection URL) so a later, asynchronous per-criterion relay can still reach it —
    see `LtiGradedResource`'s field help text for why. `context_key` gets a standalone index
    since it is now looked up on every relayed score, not just at launch.

    Both models' natural composite keys grow a fourth CharField(255)/URLField(255) column
    here (`criterion_key`) on top of three that already exist. At MySQL/utf8mb4, three such
    columns alone already use ~3060-3066 of InnoDB's 3072-byte composite index limit — the
    exact problem migrations 0d878b6/cdc8eb8 already fixed once on LtiActivityLineitem by
    dropping a column, one column short of where this change would land. A fourth column
    doesn't fit at any length, so both models get a `unique_key` field instead — a SHA-256
    digest of the real columns, computed automatically in `save()` (see
    `ags.models.compute_unique_key`) — enforcing the same uniqueness in a fixed 64 bytes
    instead of a variable, marginal ~3070+.
    """

    dependencies = [
        ('openedx_lti_tool_plugin', '0014_ltiactivitylineitem_resource_link_id'),
    ]

    operations = [
        migrations.AddField(
            model_name='ltiactivitylineitem',
            name='criterion_key',
            field=models.CharField(
                blank=True,
                default='',
                help_text=(
                    'Identifies one internal AGS line item within `problem_id` (e.g. one Muzzy '
                    'Lane rubric criterion), sourced from that line item\'s own '
                    '`resource_id`/`tag`. Empty string means "the whole problem" — today\'s '
                    'single-lineitem-per-problem semantics, unchanged for every existing row '
                    'and every block that never has more than one internal line item.'
                ),
                max_length=255,
            ),
        ),
        migrations.AddField(
            model_name='ltigradedresource',
            name='criterion_key',
            field=models.CharField(
                blank=True,
                default='',
                help_text=(
                    'Identifies one internal AGS line item within `context_key` (e.g. one Muzzy '
                    'Lane rubric criterion). Empty string ("") is the coupled/collapsed record '
                    'every launch has always created — today\'s single-score semantics, '
                    'unchanged. Deliberately has no validator (unlike `context_key`): it is not '
                    'itself a CourseKey/UsageKey, just an opaque tag borrowed from the source '
                    'line item, and adding one would be the wrong kind of check for what this '
                    'field actually holds.'
                ),
                max_length=255,
            ),
        ),
        migrations.AddField(
            model_name='ltigradedresource',
            name='resource_link_id',
            field=models.CharField(
                blank=True,
                default='',
                help_text=(
                    'LTI resource link id (the Moodle activity/placement) from the launch that '
                    'created this record. Captured here because it is only available on the '
                    'launch request, while a per-criterion relay runs later, asynchronously, '
                    'off a score change — by then the original launch request is long gone, so '
                    'anything needed at relay time that only the launch claims carry has to be '
                    'stored, not recomputed.'
                ),
                max_length=255,
            ),
        ),
        migrations.AddField(
            model_name='ltigradedresource',
            name='lineitems_url',
            field=models.URLField(
                blank=True,
                default='',
                help_text=(
                    'AGS `lineitems` collection URL from the launch claims — the endpoint used '
                    'to create additional per-criterion lineitems later. Same reasoning as '
                    '`resource_link_id`: only available at launch time, needed again later.'
                ),
                max_length=255,
            ),
        ),
        migrations.AddField(
            model_name='ltigradedresource',
            name='context_id',
            field=models.CharField(
                blank=True,
                default='',
                help_text=(
                    'LTI context claim id (the Moodle course, as opposed to `resource_link_id`, '
                    'the specific activity within it) from the launch that created this record. '
                    'Same reasoning as `resource_link_id`/`lineitems_url`: only available at '
                    'launch time, needed again later — here, to fill in the informational '
                    '(non-unique-key) `LtiActivityLineitem.context_id` field when a '
                    'per-criterion lineitem is created.'
                ),
                max_length=255,
            ),
        ),
        migrations.AlterField(
            model_name='ltigradedresource',
            name='context_key',
            field=models.CharField(
                db_index=True,
                help_text='The opaque key string of the resource.',
                max_length=255,
                validators=[openedx_lti_tool_plugin.resource_link_launch.ags.validators.validate_context_key],
            ),
        ),

        # --- LtiActivityLineitem: replace the (now 4-column, over the MySQL limit) composite
        # unique_together with a single hashed unique_key column. Add it non-unique first,
        # clear the regenerable cache table, then enforce uniqueness on an empty table.
        migrations.AddField(
            model_name='ltiactivitylineitem',
            name='unique_key',
            field=models.CharField(
                blank=True,
                default='',
                editable=False,
                help_text=(
                    'SHA-256 digest of (platform_id, resource_link_id, problem_id, '
                    'criterion_key), auto-computed in save(). This — not those four columns '
                    'directly — is what enforces uniqueness; see compute_unique_key() for why. '
                    'Lookups still filter on the real columns; only the DB-level constraint '
                    'moved to this field.'
                ),
                max_length=64,
            ),
        ),
        migrations.RunPython(clear_stale_lineitems, migrations.RunPython.noop),
        migrations.AlterUniqueTogether(
            name='ltiactivitylineitem',
            unique_together=set(),
        ),
        migrations.AlterField(
            model_name='ltiactivitylineitem',
            name='unique_key',
            field=models.CharField(
                editable=False,
                help_text=(
                    'SHA-256 digest of (platform_id, resource_link_id, problem_id, '
                    'criterion_key), auto-computed in save(). This — not those four columns '
                    'directly — is what enforces uniqueness; see compute_unique_key() for why. '
                    'Lookups still filter on the real columns; only the DB-level constraint '
                    'moved to this field.'
                ),
                max_length=64,
                unique=True,
            ),
        ),

        # --- LtiGradedResource: same fix, but backfilled instead of cleared — these rows
        # carry durable per-user state (last_score_given/last_score_maximum) worth keeping,
        # and every field the hash needs already exists on every current row.
        migrations.AddField(
            model_name='ltigradedresource',
            name='unique_key',
            field=models.CharField(
                blank=True,
                default='',
                editable=False,
                help_text=(
                    'SHA-256 digest of (lti_profile_id, context_key, lineitem, criterion_key), '
                    'auto-computed in save(). This — not those four columns directly — is what '
                    'enforces uniqueness; see compute_unique_key() for why. Lookups still '
                    'filter on the real columns; only the DB-level constraint moved to this '
                    'field.'
                ),
                max_length=64,
            ),
        ),
        migrations.RunPython(backfill_graded_resource_unique_keys, migrations.RunPython.noop),
        migrations.AlterUniqueTogether(
            name='ltigradedresource',
            unique_together=set(),
        ),
        migrations.AlterField(
            model_name='ltigradedresource',
            name='unique_key',
            field=models.CharField(
                editable=False,
                help_text=(
                    'SHA-256 digest of (lti_profile_id, context_key, lineitem, criterion_key), '
                    'auto-computed in save(). This — not those four columns directly — is what '
                    'enforces uniqueness; see compute_unique_key() for why. Lookups still '
                    'filter on the real columns; only the DB-level constraint moved to this '
                    'field.'
                ),
                max_length=64,
                unique=True,
            ),
        ),
    ]
