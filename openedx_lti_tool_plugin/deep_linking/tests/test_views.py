"""Tests views module."""
from unittest.mock import MagicMock, patch
from uuid import uuid4

from django.conf import settings
from django.http.response import Http404
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from pylti1p3.exception import LtiException

from openedx_lti_tool_plugin.apps import OpenEdxLtiToolPluginConfig as app_config
from openedx_lti_tool_plugin.deep_linking.exceptions import DeepLinkingException
from openedx_lti_tool_plugin.deep_linking.forms import DeepLinkingForm
from openedx_lti_tool_plugin.deep_linking.tests import MODULE_PATH
from openedx_lti_tool_plugin.deep_linking.views import (
    CUSTOM_CLAIM,
    DEEP_LINKING_SETTINGS_CLAIM,
    TARGET_LINK_URI_CLAIM,
    DeepLinkingFormView,
    DeepLinkingView,
    accepts_multiple,
    get_scoped_course_id,
)

MODULE_PATH = f'{MODULE_PATH}.views'

COURSE_KEY = 'course-v1:Org+Course+Run'
BLOCK_KEY = 'block-v1:Org+Course+Run+type@problem+block@abc'


class TestGetScopedCourseId(TestCase):
    """Test get_scoped_course_id function."""

    def test_from_custom_course_id(self):
        """Returns the course_id custom parameter."""
        data = {CUSTOM_CLAIM: {'course_id': COURSE_KEY}}
        self.assertEqual(get_scoped_course_id(data), COURSE_KEY)

    def test_from_custom_resource_id(self):
        """Falls back to the resourceId custom parameter."""
        data = {CUSTOM_CLAIM: {'resourceId': COURSE_KEY}}
        self.assertEqual(get_scoped_course_id(data), COURSE_KEY)

    def test_from_target_link_uri(self):
        """Falls back to the course in the target_link_uri launch path."""
        data = {TARGET_LINK_URI_CLAIM: f'https://lms.test/openedx_lti_tool_plugin/1.3/launch/{COURSE_KEY}'}
        self.assertEqual(get_scoped_course_id(data), COURSE_KEY)

    def test_ignores_block_target(self):
        """A block-level target is not a CourseKey, so returns ''."""
        data = {TARGET_LINK_URI_CLAIM: f'https://lms.test/openedx_lti_tool_plugin/1.3/launch/{BLOCK_KEY}'}
        self.assertEqual(get_scoped_course_id(data), '')

    def test_empty_when_absent(self):
        """Returns '' when no course can be resolved."""
        self.assertEqual(get_scoped_course_id({}), '')


class TestAcceptsMultiple(TestCase):
    """Test accepts_multiple function."""

    def test_true_boolean(self):
        """Returns True for a JSON boolean true."""
        data = {DEEP_LINKING_SETTINGS_CLAIM: {'accept_multiple': True}}
        self.assertTrue(accepts_multiple(data))

    def test_true_string(self):
        """Returns True for the string forms some platforms send."""
        for raw_value in ('true', 'True', ' TRUE '):
            with self.subTest(raw_value=raw_value):
                data = {DEEP_LINKING_SETTINGS_CLAIM: {'accept_multiple': raw_value}}
                self.assertTrue(accepts_multiple(data))

    def test_false_boolean(self):
        """Returns False for a JSON boolean false."""
        data = {DEEP_LINKING_SETTINGS_CLAIM: {'accept_multiple': False}}
        self.assertFalse(accepts_multiple(data))

    def test_false_string(self):
        """Returns False for the string 'false'."""
        data = {DEEP_LINKING_SETTINGS_CLAIM: {'accept_multiple': 'false'}}
        self.assertFalse(accepts_multiple(data))

    def test_property_absent(self):
        """Returns False when the property is not in the settings claim."""
        self.assertFalse(accepts_multiple({DEEP_LINKING_SETTINGS_CLAIM: {}}))

    def test_claim_absent(self):
        """Returns False when the settings claim is not in the launch data."""
        self.assertFalse(accepts_multiple({}))

    def test_claim_null(self):
        """Returns False when the settings claim is null."""
        self.assertFalse(accepts_multiple({DEEP_LINKING_SETTINGS_CLAIM: None}))

    def test_claim_not_an_object(self):
        """Returns False without raising when the settings claim is not an object."""
        for claim in ('true', ['accept_multiple'], 1):
            with self.subTest(claim=claim):
                self.assertFalse(accepts_multiple({DEEP_LINKING_SETTINGS_CLAIM: claim}))


@patch.object(DeepLinkingView, 'get_message')
@patch(f'{MODULE_PATH}.validate_deep_linking_message')
@patch(f'{MODULE_PATH}.redirect')
class TestDeepLinkingViewPost(TestCase):
    """Test ResourceLinkLaunchView.post method."""

    def setUp(self):
        """Set up test fixtures."""
        super().setUp()
        self.view_class = DeepLinkingView
        self.request = RequestFactory().post(reverse('1.3:deep-linking:root'))

    def test_with_deep_linking_request(
        self,
        redirect_mock: MagicMock,
        validate_deep_linking_message_mock: MagicMock,
        get_message_mock: MagicMock,
    ):
        """Test with deep linking request (happy path)."""
        self.assertEqual(
            self.view_class.as_view()(self.request),
            redirect_mock.return_value,
        )
        get_message_mock.assert_called_once_with(self.request)
        validate_deep_linking_message_mock.assert_called_once_with(get_message_mock())
        get_message_mock().get_launch_id.assert_called_once_with()
        get_message_mock().get_launch_id().replace.assert_called_once_with('lti1p3-launch-', '')
        redirect_mock.assert_called_once_with(
            f'{app_config.name}:1.3:deep-linking:form',
            launch_id=get_message_mock().get_launch_id().replace(),
        )

    @patch.object(DeepLinkingView, 'http_response_error')
    def test_with_lti_exception(
        self,
        http_response_error_mock: MagicMock,
        redirect_mock: MagicMock,
        validate_deep_linking_message_mock: MagicMock,
        get_message_mock: MagicMock,
    ):
        """Test with LtiException."""
        exception = LtiException('Error message')
        get_message_mock.side_effect = exception

        self.assertEqual(
            self.view_class.as_view()(self.request),
            http_response_error_mock.return_value,
        )
        get_message_mock.assert_called_once_with(self.request)
        validate_deep_linking_message_mock.assert_not_called()
        redirect_mock.assert_not_called()
        http_response_error_mock.assert_called_once_with(exception)

    @patch.object(DeepLinkingView, 'http_response_error')
    def test_with_deep_linking_exception(
        self,
        http_response_error_mock: MagicMock,
        redirect_mock: MagicMock,
        validate_deep_linking_message_mock: MagicMock,
        get_message_mock: MagicMock,
    ):
        """Test with DeepLinkingException."""
        exception = DeepLinkingException('Error message')
        validate_deep_linking_message_mock.side_effect = exception

        self.assertEqual(
            self.view_class.as_view()(self.request),
            http_response_error_mock.return_value,
        )
        get_message_mock.assert_called_once_with(self.request)
        validate_deep_linking_message_mock.assert_called_once_with(get_message_mock())
        redirect_mock.assert_not_called()
        http_response_error_mock.assert_called_once_with(exception)

    @override_settings(OLTITP_ENABLE_LTI_TOOL=False)
    def test_with_lti_disabled(self, *args):
        """Test raise 404 response when plugin is disabled."""
        with self.assertRaises(Http404):
            self.view_class.as_view()(self.request)


class TestDeepLinkingFormView(TestCase):
    """Test DeepLinkingFormView class."""

    def test_class_attributes(self):
        """Test class attributes."""
        self.assertEqual(DeepLinkingFormView.form_class, DeepLinkingForm)


@patch.object(DeepLinkingFormView, 'get_message_from_cache')
@patch(f'{MODULE_PATH}.validate_deep_linking_message')
@patch(f'{MODULE_PATH}.configuration_helpers')
class TestDeepLinkingFormViewGet(TestCase):
    """Test DeepLinkingFormView.get method."""

    def setUp(self):
        """Set up test fixtures."""
        super().setUp()
        self.view_class = DeepLinkingFormView
        self.launch_id = uuid4()
        self.request = RequestFactory().get(
            reverse('1.3:deep-linking:form', args=[self.launch_id]),
        )

    @patch(f'{MODULE_PATH}.accepts_multiple')
    @patch(f'{MODULE_PATH}.get_scoped_course_id')
    @patch(f'{MODULE_PATH}.render')
    def test_with_deep_linking_request(
        self,
        render_mock: MagicMock,
        get_scoped_course_id_mock: MagicMock,
        accepts_multiple_mock: MagicMock,
        configuration_helpers_mock: MagicMock,
        validate_deep_linking_message_mock: MagicMock,
        get_message_from_cache_mock: MagicMock,
    ):
        """Test with deep linking request (happy path)."""
        self.assertEqual(
            self.view_class.as_view()(self.request, self.launch_id),
            render_mock.return_value,
        )
        get_message_from_cache_mock.assert_called_once_with(self.request, self.launch_id)
        validate_deep_linking_message_mock.assert_called_once_with(get_message_from_cache_mock())
        configuration_helpers_mock().get_value.assert_called_once_with(
            'OLTITP_DEEP_LINKING_FORM_TEMPLATE',
            settings.OLTITP_DEEP_LINKING_FORM_TEMPLATE,
        )
        get_scoped_course_id_mock.assert_called_once_with(
            get_message_from_cache_mock().get_launch_data(),
        )
        accepts_multiple_mock.assert_called_once_with(
            get_message_from_cache_mock().get_launch_data(),
        )
        render_mock.assert_called_once_with(
            self.request,
            configuration_helpers_mock().get_value(),
            {
                'launch_id': self.launch_id,
                'course_id': get_scoped_course_id_mock.return_value,
                'accept_multiple': accepts_multiple_mock.return_value,
            },
        )

    @patch.object(DeepLinkingFormView, 'http_response_error')
    def test_with_lti_exception(
        self,
        http_response_error_mock: MagicMock,
        configuration_helpers_mock: MagicMock,
        validate_deep_linking_message_mock: MagicMock,
        get_message_from_cache_mock: MagicMock,
    ):
        """Test with LtiException."""
        exception = LtiException('Error message')
        get_message_from_cache_mock.side_effect = exception

        self.assertEqual(
            self.view_class.as_view()(self.request, self.launch_id),
            http_response_error_mock.return_value,
        )
        get_message_from_cache_mock.assert_called_once_with(self.request, self.launch_id)
        validate_deep_linking_message_mock.assert_not_called()
        configuration_helpers_mock().get_value.assert_not_called()
        http_response_error_mock.assert_called_once_with(exception)

    @patch.object(DeepLinkingFormView, 'http_response_error')
    def test_with_deep_linking_exception(
        self,
        http_response_error_mock: MagicMock,
        configuration_helpers_mock: MagicMock,
        validate_deep_linking_message_mock: MagicMock,
        get_message_from_cache_mock: MagicMock,
    ):
        """Test with DeepLinkingException."""
        exception = DeepLinkingException('Error message')
        validate_deep_linking_message_mock.side_effect = exception

        self.assertEqual(
            self.view_class.as_view()(self.request, self.launch_id),
            http_response_error_mock.return_value,
        )
        get_message_from_cache_mock.assert_called_once_with(self.request, self.launch_id)
        validate_deep_linking_message_mock.assert_called_once_with(get_message_from_cache_mock())
        configuration_helpers_mock().get_value.assert_not_called()
        http_response_error_mock.assert_called_once_with(exception)

    @override_settings(OLTITP_ENABLE_LTI_TOOL=False)
    def test_with_lti_disabled(self, *args):
        """Test raise 404 response when plugin is disabled."""
        with self.assertRaises(Http404):
            self.view_class.as_view()(self.request)


@patch.object(DeepLinkingFormView, 'get_message_from_cache')
@patch(f'{MODULE_PATH}.validate_deep_linking_message')
@patch.object(DeepLinkingFormView, 'form_class')
class TestDeepLinkingFormViewPost(TestCase):
    """Test DeepLinkingFormView.post method."""

    def setUp(self):
        """Set up test fixtures."""
        super().setUp()
        self.view_class = DeepLinkingFormView
        self.launch_id = uuid4()
        self.request = RequestFactory().post(
            reverse('1.3:deep-linking:form', args=[self.launch_id]),
        )

    @patch(f'{MODULE_PATH}.accepts_multiple')
    @patch(f'{MODULE_PATH}.HttpResponse')
    def test_with_deep_linking_request(
        self,
        http_response_mock: MagicMock,
        accepts_multiple_mock: MagicMock,
        form_class_mock: MagicMock,
        validate_deep_linking_message_mock: MagicMock,
        get_message_from_cache_mock: MagicMock,
    ):
        """Test with deep linking request (happy path)."""
        form_class_mock.return_value.cleaned_data = {
            'deep_link_resources': [
                MagicMock(
                    url='http://example.com',
                    title='Test',
                )
            ]
        }

        self.assertEqual(
            self.view_class.as_view()(self.request, self.launch_id),
            http_response_mock.return_value,
        )
        get_message_from_cache_mock.assert_called_once_with(self.request, self.launch_id)
        validate_deep_linking_message_mock.assert_called_once_with(get_message_from_cache_mock())
        form_class_mock.assert_called_once_with(
            self.request.POST,
            accept_multiple=accepts_multiple_mock.return_value,
        )
        form_class_mock().is_valid.assert_called_once_with()
        get_message_from_cache_mock().get_deep_link.assert_called_once_with()
        get_message_from_cache_mock().get_deep_link().output_response_form.assert_called_once_with(
            form_class_mock().cleaned_data['deep_link_resources'],
        )
        http_response_mock.assert_called_once_with(
            get_message_from_cache_mock().get_deep_link().output_response_form(),
        )

    @patch(f'{MODULE_PATH}.accepts_multiple')
    @patch.object(DeepLinkingFormView, 'http_response_error')
    def test_with_invalid_form(
        self,
        http_response_error_mock: MagicMock,
        accepts_multiple_mock: MagicMock,
        form_class_mock: MagicMock,
        validate_deep_linking_message_mock: MagicMock,
        get_message_from_cache_mock: MagicMock,
    ):
        """Test with invalid form."""
        form_class_mock.return_value.is_valid.return_value = False

        self.assertEqual(
            self.view_class.as_view()(self.request, self.launch_id),
            http_response_error_mock.return_value,
        )
        get_message_from_cache_mock.assert_called_once_with(self.request, self.launch_id)
        validate_deep_linking_message_mock.assert_called_once_with(get_message_from_cache_mock())
        form_class_mock.assert_called_once_with(
            self.request.POST,
            accept_multiple=accepts_multiple_mock.return_value,
        )
        form_class_mock().is_valid.assert_called_once_with()
        get_message_from_cache_mock().get_deep_link.assert_not_called()
        get_message_from_cache_mock().get_deep_link().output_response_form.assert_not_called()
        http_response_error_mock.assert_called_once()

    @patch.object(DeepLinkingFormView, 'http_response_error')
    def test_with_lti_exception(
        self,
        http_response_error_mock: MagicMock,
        form_class_mock: MagicMock,
        validate_deep_linking_message_mock: MagicMock,
        get_message_from_cache_mock: MagicMock,
    ):
        """Test with LtiException."""
        exception = LtiException('Error message')
        get_message_from_cache_mock.side_effect = exception

        self.assertEqual(
            self.view_class.as_view()(self.request, self.launch_id),
            http_response_error_mock.return_value,
        )
        get_message_from_cache_mock.assert_called_once_with(self.request, self.launch_id)
        validate_deep_linking_message_mock.assert_not_called()
        form_class_mock.assert_not_called()
        form_class_mock().is_valid.assert_not_called()
        get_message_from_cache_mock.return_value.get_deep_link.assert_not_called()
        get_message_from_cache_mock.return_value.get_deep_link().output_response_form.assert_not_called()
        http_response_error_mock.assert_called_once_with(exception)

    @patch.object(DeepLinkingFormView, 'http_response_error')
    def test_with_deep_linking_exception(
        self,
        http_response_error_mock: MagicMock,
        form_class_mock: MagicMock,
        validate_deep_linking_message_mock: MagicMock,
        get_message_from_cache_mock: MagicMock,
    ):
        """Test with DeepLinkingException."""
        exception = DeepLinkingException('Error message')
        validate_deep_linking_message_mock.side_effect = exception

        self.assertEqual(
            self.view_class.as_view()(self.request, self.launch_id),
            http_response_error_mock.return_value,
        )
        get_message_from_cache_mock.assert_called_once_with(self.request, self.launch_id)
        validate_deep_linking_message_mock.assert_called_once_with(get_message_from_cache_mock())
        form_class_mock.assert_not_called()
        form_class_mock().is_valid.assert_not_called()
        get_message_from_cache_mock().get_deep_link.assert_not_called()
        get_message_from_cache_mock().get_deep_link().output_response_form.assert_not_called()
        http_response_error_mock.assert_called_once_with(exception)

    @override_settings(OLTITP_ENABLE_LTI_TOOL=False)
    def test_with_lti_disabled(self, *args):
        """Test raise 404 response when plugin is disabled."""
        with self.assertRaises(Http404):
            self.view_class.as_view()(self.request)
