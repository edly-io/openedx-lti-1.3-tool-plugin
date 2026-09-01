"""Tests tasks module."""
from unittest.mock import MagicMock, patch

from django.test import TestCase
from pylti1p3.exception import LtiException

from openedx_lti_tool_plugin.resource_link_launch.ags.tasks import (
    get_external_user_id,
    get_gradable_blocks,
    get_gradable_blocks_for_resource,
    get_multi_line_item_lti_configuration,
    send_score_updates,
)
from openedx_lti_tool_plugin.resource_link_launch.ags.tests import MODULE_PATH
from openedx_lti_tool_plugin.tests import COURSE_ID, USAGE_KEY

MODULE_PATH = f'{MODULE_PATH}.tasks'


class TestGetGradableBlocks(TestCase):
    """Test get_gradable_blocks function."""

    @patch(f'{MODULE_PATH}.modulestore')
    def test_filters_scored_gradable_blocks(self, modulestore_mock: MagicMock):
        """Returns only scored blocks that are graded or weighted (any block type)."""
        course_key = MagicMock()
        problem = MagicMock(has_score=True, graded=True, weight=None)            # native problem
        lti_tool = MagicMock(has_score=True, graded=True, weight=1.0)            # consumed LTI tool
        weighted_only = MagicMock(has_score=True, graded=False, weight=2.0)      # scored + weighted
        ungraded_scored = MagicMock(has_score=True, graded=False, weight=None)   # excluded
        content = MagicMock(has_score=False, graded=True, weight=1.0)            # excluded (no score)
        modulestore_mock().get_items.return_value = [
            problem, lti_tool, weighted_only, ungraded_scored, content,
        ]

        result = get_gradable_blocks(course_key)

        self.assertEqual(result, [problem, lti_tool, weighted_only])
        modulestore_mock().get_items.assert_called_once_with(course_key)


class TestGetGradableBlocksForResource(TestCase):
    """Test get_gradable_blocks_for_resource function."""

    @patch(f'{MODULE_PATH}.modulestore')
    def test_course_resource(self, modulestore_mock: MagicMock):
        """A course resource returns all gradable blocks in the course."""
        problem = MagicMock(has_score=True, graded=True, weight=None)
        modulestore_mock().get_items.return_value = [problem]

        result = get_gradable_blocks_for_resource('course-v1:Org+Course+Run')

        self.assertEqual(result, [problem])

    @patch(f'{MODULE_PATH}.modulestore')
    def test_container_block_resource(self, modulestore_mock: MagicMock):
        """A container block (unit) returns the gradable blocks inside it."""
        problem1 = MagicMock(has_score=True, graded=True, weight=None)
        problem1.get_children.return_value = []
        problem2 = MagicMock(has_score=True, graded=False, weight=1.0)
        problem2.get_children.return_value = []
        unit = MagicMock()
        unit.get_children.return_value = [problem1, problem2]
        modulestore_mock().get_item.return_value = unit

        result = get_gradable_blocks_for_resource(
            'block-v1:Org+Course+Run+type@vertical+block@u1',
        )

        self.assertEqual(result, [problem1, problem2])

    @patch(f'{MODULE_PATH}.modulestore')
    def test_leaf_block_resource_returns_empty(self, modulestore_mock: MagicMock):
        """A single leaf block returns [] — its own coupled lineitem already covers it."""
        leaf = MagicMock()
        leaf.get_children.return_value = []
        modulestore_mock().get_item.return_value = leaf

        result = get_gradable_blocks_for_resource(
            'block-v1:Org+Course+Run+type@problem+block@p1',
        )

        self.assertEqual(result, [])


@patch(f'{MODULE_PATH}.relay_criterion_scores')
@patch(f'{MODULE_PATH}.get_multi_line_item_lti_configuration')
@patch(f'{MODULE_PATH}.course_grade_factory')
@patch(f'{MODULE_PATH}.CourseKey')
@patch(f'{MODULE_PATH}.LtiGradedResource')
@patch(f'{MODULE_PATH}.modulestore')
@patch(f'{MODULE_PATH}.get_user_model')
@patch(f'{MODULE_PATH}.UsageKey')
@patch(f'{MODULE_PATH}.LtiProfile')
class TestSendScoreUpdates(TestCase):
    """Test send_score_updates function."""

    def setUp(self):
        """Set up test fixtures."""
        self.user = MagicMock(id=1)
        self.user_id = 1
        self.course_id = COURSE_ID
        self.problem_id = USAGE_KEY
        self.course_grade = MagicMock()
        self.course_grade.score_for_block.return_value = (1, 1)
        self.graded_resource = MagicMock()

    def _leaf_then_course(self):
        """Return a get_item side effect: a leaf whose parent is the course."""
        leaf = MagicMock()
        leaf.location.block_type = 'problem'
        leaf.parent = MagicMock()
        course_block = MagicMock()
        course_block.location.block_type = 'course'
        return leaf, course_block

    def test_publishes_aggregate_for_each_launched_ancestor(
        self,
        lti_profile_mock: MagicMock,
        usage_key_mock: MagicMock,  # pylint: disable=unused-argument
        get_user_model_mock: MagicMock,
        modulestore_mock: MagicMock,
        lti_graded_resource_mock: MagicMock,
        course_key_mock: MagicMock,  # pylint: disable=unused-argument
        course_grade_factory_mock: MagicMock,
        get_multi_line_item_lti_configuration_mock: MagicMock,
        relay_criterion_scores_mock: MagicMock,
    ):
        """Walks the block and its ancestors, posting each level's aggregate score.

        Not a per-criterion block (get_multi_line_item_lti_configuration returns None, the
        same as any native problem or declarative-mode lti_consumer block would) — exercises
        today's unchanged collapsed-publish behavior.
        """
        lti_profile_mock.objects.filter.return_value.first.return_value = MagicMock()
        get_user_model_mock.return_value.objects.filter.return_value.first.return_value = self.user
        course_grade_factory_mock.return_value.read.return_value = self.course_grade
        get_multi_line_item_lti_configuration_mock.return_value = None
        leaf, course_block = self._leaf_then_course()
        modulestore_mock.return_value.get_item.side_effect = [leaf, course_block]
        all_from_user_id_result = lti_graded_resource_mock.objects.all_from_user_id.return_value
        all_from_user_id_result.filter.return_value = [self.graded_resource]

        self.assertIsNone(send_score_updates(self.user_id, self.course_id, self.problem_id))

        lti_graded_resource_mock.objects.all_from_user_id.assert_called_once_with(
            user_id=self.user_id,
            context_key=str(leaf.location),
        )
        all_from_user_id_result.filter.assert_called_once_with(criterion_key='')
        self.course_grade.score_for_block.assert_called_once_with(leaf.location)
        self.graded_resource.publish_score.assert_called_once_with(1, 1)
        relay_criterion_scores_mock.assert_not_called()

    def test_relays_per_criterion_for_a_multi_line_item_block(
        self,
        lti_profile_mock: MagicMock,
        usage_key_mock: MagicMock,  # pylint: disable=unused-argument
        get_user_model_mock: MagicMock,
        modulestore_mock: MagicMock,
        lti_graded_resource_mock: MagicMock,
        course_key_mock: MagicMock,  # pylint: disable=unused-argument
        course_grade_factory_mock: MagicMock,
        get_multi_line_item_lti_configuration_mock: MagicMock,
        relay_criterion_scores_mock: MagicMock,
    ):
        """A block opted into per-criterion relay skips the collapsed publish entirely.

        This is the corruption-fix regression test: the coupled record must never receive the
        collapsed course_grade value once a block is in per-criterion mode, since that value
        would be wrong for every one of its real per-criterion Moodle columns.
        """
        lti_profile = MagicMock()
        lti_profile_mock.objects.filter.return_value.first.return_value = lti_profile
        get_user_model_mock.return_value.objects.filter.return_value.first.return_value = self.user
        course_grade_factory_mock.return_value.read.return_value = self.course_grade
        lti_configuration = MagicMock()
        get_multi_line_item_lti_configuration_mock.return_value = lti_configuration
        leaf, course_block = self._leaf_then_course()
        modulestore_mock.return_value.get_item.side_effect = [leaf, course_block]
        all_from_user_id_result = lti_graded_resource_mock.objects.all_from_user_id.return_value
        all_from_user_id_result.filter.return_value = [self.graded_resource]

        self.assertIsNone(send_score_updates(self.user_id, self.course_id, self.problem_id))

        get_multi_line_item_lti_configuration_mock.assert_called_once_with(leaf, lti_profile)
        relay_criterion_scores_mock.assert_called_once_with(
            lti_profile, self.graded_resource, lti_configuration, leaf,
        )
        self.course_grade.score_for_block.assert_not_called()
        self.graded_resource.publish_score.assert_not_called()

    def test_skips_location_with_no_coupled_resource(
        self,
        lti_profile_mock: MagicMock,
        usage_key_mock: MagicMock,  # pylint: disable=unused-argument
        get_user_model_mock: MagicMock,
        modulestore_mock: MagicMock,
        lti_graded_resource_mock: MagicMock,
        course_key_mock: MagicMock,  # pylint: disable=unused-argument
        course_grade_factory_mock: MagicMock,
        get_multi_line_item_lti_configuration_mock: MagicMock,
        relay_criterion_scores_mock: MagicMock,
    ):
        """A location this user never launched through the bridge is skipped entirely."""
        lti_profile_mock.objects.filter.return_value.first.return_value = MagicMock()
        get_user_model_mock.return_value.objects.filter.return_value.first.return_value = self.user
        course_grade_factory_mock.return_value.read.return_value = self.course_grade
        leaf, course_block = self._leaf_then_course()
        modulestore_mock.return_value.get_item.side_effect = [leaf, course_block]
        all_from_user_id_result = lti_graded_resource_mock.objects.all_from_user_id.return_value
        all_from_user_id_result.filter.return_value = []

        self.assertIsNone(send_score_updates(self.user_id, self.course_id, self.problem_id))

        get_multi_line_item_lti_configuration_mock.assert_not_called()
        relay_criterion_scores_mock.assert_not_called()
        self.course_grade.score_for_block.assert_not_called()

    def test_without_lti_profile(
        self,
        lti_profile_mock: MagicMock,
        usage_key_mock: MagicMock,  # pylint: disable=unused-argument
        get_user_model_mock: MagicMock,  # pylint: disable=unused-argument
        modulestore_mock: MagicMock,
        lti_graded_resource_mock: MagicMock,  # pylint: disable=unused-argument
        course_key_mock: MagicMock,  # pylint: disable=unused-argument
        course_grade_factory_mock: MagicMock,  # pylint: disable=unused-argument
        get_multi_line_item_lti_configuration_mock: MagicMock,  # pylint: disable=unused-argument
        relay_criterion_scores_mock: MagicMock,  # pylint: disable=unused-argument
    ):
        """Short-circuits when the user has no LtiProfile."""
        lti_profile_mock.objects.filter.return_value.first.return_value = None

        self.assertIsNone(send_score_updates(self.user_id, self.course_id, self.problem_id))

        modulestore_mock.return_value.get_item.assert_not_called()
        self.graded_resource.publish_score.assert_not_called()


class TestGetMultiLineItemLtiConfiguration(TestCase):
    """Test get_multi_line_item_lti_configuration function."""

    def setUp(self):
        """Set up test fixtures."""
        self.lti_profile = MagicMock(platform_id='https://platform.example', client_id='client-1')

    def test_non_lti_consumer_block_returns_none(self):
        """A block that isn't lti_consumer never attempts the lti_consumer import at all."""
        block = MagicMock(category='problem')

        self.assertIsNone(get_multi_line_item_lti_configuration(block, self.lti_profile))

    def test_lti_consumer_not_installed_returns_none(self):
        """No lti_consumer plugin on this Open edX instance — a clean no-op, not a crash.

        `sys.modules['lti_consumer.models'] = None` is Python's own documented mechanism for
        forcing an import to raise ImportError, used here so this test is deterministic
        regardless of whether lti_consumer actually happens to be installed in whatever
        environment runs this suite.
        """
        block = MagicMock(category='lti_consumer')

        with patch.dict('sys.modules', {'lti_consumer.models': None}):
            result = get_multi_line_item_lti_configuration(block, self.lti_profile)

        self.assertIsNone(result)

    def test_declarative_ags_mode_returns_none(self):
        """Programmatic is a precondition — declarative mode never qualifies."""
        block = MagicMock(category='lti_consumer')
        lti_configuration = MagicMock()
        lti_configuration.get_lti_advantage_ags_mode.return_value = 'declarative'
        lti_consumer_models = MagicMock()
        lti_consumer_models.LtiConfiguration.objects.filter.return_value.first.return_value = lti_configuration

        with patch.dict('sys.modules', {'lti_consumer.models': lti_consumer_models}):
            result = get_multi_line_item_lti_configuration(block, self.lti_profile)

        self.assertIsNone(result)

    def test_no_lti_configuration_returns_none(self):
        """The block claims lti_consumer but has no LtiConfiguration row at all."""
        block = MagicMock(category='lti_consumer')
        lti_consumer_models = MagicMock()
        lti_consumer_models.LtiConfiguration.objects.filter.return_value.first.return_value = None

        with patch.dict('sys.modules', {'lti_consumer.models': lti_consumer_models}):
            result = get_multi_line_item_lti_configuration(block, self.lti_profile)

        self.assertIsNone(result)

    @patch(f'{MODULE_PATH}.LtiToolConfiguration')
    @patch('pylti1p3.contrib.django.DjangoDbToolConf')
    def test_coupled_tool_configuration_returns_none(
        self,
        django_db_tool_conf_mock: MagicMock,  # pylint: disable=unused-argument
        lti_tool_configuration_mock: MagicMock,
    ):
        """Programmatic ags_mode alone is not enough — the Moodle tool must also opt in."""
        block = MagicMock(category='lti_consumer')
        lti_configuration = MagicMock()
        lti_configuration.get_lti_advantage_ags_mode.return_value = 'programmatic'
        lti_consumer_models = MagicMock()
        lti_consumer_models.LtiConfiguration.objects.filter.return_value.first.return_value = lti_configuration
        lti_tool_configuration_mock.objects.get.return_value.uses_per_problem_passback.return_value = False

        with patch.dict('sys.modules', {'lti_consumer.models': lti_consumer_models}):
            result = get_multi_line_item_lti_configuration(block, self.lti_profile)

        self.assertIsNone(result)

    @patch(f'{MODULE_PATH}.LtiToolConfiguration')
    @patch('pylti1p3.contrib.django.DjangoDbToolConf')
    def test_no_matching_lti_tool_configuration_returns_none(
        self,
        django_db_tool_conf_mock: MagicMock,  # pylint: disable=unused-argument
        lti_tool_configuration_mock: MagicMock,
    ):
        """No LtiToolConfiguration exists for this launch's iss/aud at all."""
        block = MagicMock(category='lti_consumer')
        lti_configuration = MagicMock()
        lti_configuration.get_lti_advantage_ags_mode.return_value = 'programmatic'
        lti_consumer_models = MagicMock()
        lti_consumer_models.LtiConfiguration.objects.filter.return_value.first.return_value = lti_configuration
        lti_tool_configuration_mock.DoesNotExist = Exception
        lti_tool_configuration_mock.objects.get.side_effect = lti_tool_configuration_mock.DoesNotExist

        with patch.dict('sys.modules', {'lti_consumer.models': lti_consumer_models}):
            result = get_multi_line_item_lti_configuration(block, self.lti_profile)

        self.assertIsNone(result)

    @patch(f'{MODULE_PATH}.LtiToolConfiguration')
    @patch('pylti1p3.contrib.django.DjangoDbToolConf')
    def test_lti_exception_from_get_lti_tool_returns_none(
        self,
        django_db_tool_conf_mock: MagicMock,
        lti_tool_configuration_mock: MagicMock,  # pylint: disable=unused-argument
    ):
        """get_lti_tool raising LtiException is caught, not left to crash the Celery task."""
        block = MagicMock(category='lti_consumer')
        lti_configuration = MagicMock()
        lti_configuration.get_lti_advantage_ags_mode.return_value = 'programmatic'
        lti_consumer_models = MagicMock()
        lti_consumer_models.LtiConfiguration.objects.filter.return_value.first.return_value = lti_configuration
        django_db_tool_conf_mock.return_value.get_lti_tool.side_effect = LtiException('no registration')

        with patch.dict('sys.modules', {'lti_consumer.models': lti_consumer_models}):
            result = get_multi_line_item_lti_configuration(block, self.lti_profile)

        self.assertIsNone(result)

    @patch(f'{MODULE_PATH}.LtiToolConfiguration')
    @patch('pylti1p3.contrib.django.DjangoDbToolConf')
    def test_returns_lti_configuration_when_all_conditions_met(
        self,
        django_db_tool_conf_mock: MagicMock,  # pylint: disable=unused-argument
        lti_tool_configuration_mock: MagicMock,
    ):
        """lti_consumer block + programmatic ags_mode + PER_PROBLEM tool config → qualifies."""
        block = MagicMock(category='lti_consumer')
        lti_configuration = MagicMock()
        lti_configuration.get_lti_advantage_ags_mode.return_value = 'programmatic'
        lti_consumer_models = MagicMock()
        lti_consumer_models.LtiConfiguration.objects.filter.return_value.first.return_value = lti_configuration
        lti_tool_configuration_mock.objects.get.return_value.uses_per_problem_passback.return_value = True

        with patch.dict('sys.modules', {'lti_consumer.models': lti_consumer_models}):
            result = get_multi_line_item_lti_configuration(block, self.lti_profile)

        self.assertEqual(result, lti_configuration)


class TestGetExternalUserId(TestCase):
    """Test get_external_user_id function."""

    def setUp(self):
        """Set up test fixtures."""
        self.lti_profile = MagicMock()
        self.lti_profile.user.id = 42

    def test_lti_consumer_not_installed_returns_none(self):
        """No lti_consumer plugin — a clean no-op, not a crash."""
        with patch.dict('sys.modules', {'lti_consumer.plugin.compat': None}):
            result = get_external_user_id(self.lti_profile)

        self.assertIsNone(result)

    def test_returns_external_user_id(self):
        """Resolves via the same compat helper xblock-lti-consumer itself uses.

        `from lti_consumer.plugin import compat` needs every level of that dotted path
        resolvable, not just the leaf — unlike a plain `from lti_consumer.models import X`,
        where only the leaf module needs to be in `sys.modules`. Verified empirically before
        writing this: stubbing only the leaf here reliably raises ImportError instead of
        reaching the code under test, silently turning this into a no-op test.
        """
        external_id = MagicMock(external_user_id='ext-123')
        compat_mock = MagicMock()
        compat_mock.batch_get_or_create_externalids.return_value = {42: external_id}

        with patch.dict('sys.modules', {
            'lti_consumer': MagicMock(),
            'lti_consumer.plugin': MagicMock(),
            'lti_consumer.plugin.compat': compat_mock,
        }):
            result = get_external_user_id(self.lti_profile)

        compat_mock.batch_get_or_create_externalids.assert_called_once_with([self.lti_profile.user])
        self.assertEqual(result, 'ext-123')

    def test_no_external_id_returns_none(self):
        """The batch lookup not returning an entry for this user is a clean no-op."""
        compat_mock = MagicMock()
        compat_mock.batch_get_or_create_externalids.return_value = {}

        with patch.dict('sys.modules', {
            'lti_consumer': MagicMock(),
            'lti_consumer.plugin': MagicMock(),
            'lti_consumer.plugin.compat': compat_mock,
        }):
            result = get_external_user_id(self.lti_profile)

        self.assertIsNone(result)
