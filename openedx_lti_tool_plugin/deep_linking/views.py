"""Django Views."""
from typing import Union
from uuid import uuid4

from django.conf import settings
from django.http import HttpResponse
from django.http.request import HttpRequest
from django.shortcuts import redirect, render
from django.utils.decorators import method_decorator
from django.utils.translation import gettext as _
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.csrf import csrf_exempt
from opaque_keys import InvalidKeyError
from opaque_keys.edx.keys import CourseKey
from pylti1p3.exception import LtiException

from openedx_lti_tool_plugin.apps import OpenEdxLtiToolPluginConfig as app_config
from openedx_lti_tool_plugin.deep_linking.exceptions import DeepLinkingException
from openedx_lti_tool_plugin.deep_linking.forms import DeepLinkingForm
from openedx_lti_tool_plugin.deep_linking.utils import validate_deep_linking_message
from openedx_lti_tool_plugin.edxapp_wrapper.site_configuration_module import configuration_helpers
from openedx_lti_tool_plugin.http import LoggedHttpResponseBadRequest
from openedx_lti_tool_plugin.views import LTIToolView

CUSTOM_CLAIM = 'https://purl.imsglobal.org/spec/lti/claim/custom'
TARGET_LINK_URI_CLAIM = 'https://purl.imsglobal.org/spec/lti/claim/target_link_uri'


def get_scoped_course_id(launch_data: dict) -> str:
    """Return the course the picker should be scoped to, or '' for the full course list.

    Looks first for a ``course_id`` (or ``resourceId``) custom parameter, then falls back
    to the course in the launch's ``target_link_uri`` (``.../launch/<course_id>``). Only a
    valid CourseKey is returned, so block-level or unrelated targets are ignored and the
    picker falls back to the full (access-scoped) course list.

    Args:
        launch_data: Deep linking launch message data.

    Returns:
        A course ID string, or '' when none can be resolved.

    """
    custom = launch_data.get(CUSTOM_CLAIM, {}) or {}
    candidate = custom.get('course_id') or custom.get('resourceId') or ''

    if not candidate:
        target = launch_data.get(TARGET_LINK_URI_CLAIM, '') or ''
        if '/launch/' in target:
            candidate = target.split('/launch/', 1)[1].split('?', 1)[0].strip('/')

    try:
        CourseKey.from_string(candidate)
    except InvalidKeyError:
        return ''

    return candidate


@method_decorator([csrf_exempt, xframe_options_exempt], name='dispatch')
class DeepLinkingView(LTIToolView):
    """Deep Linking View.

    This view handles the initial LtiDeepLinkingRequest from the platform.

    .. _LTI Deep Linking Specification - Workflow:
        https://www.imsglobal.org/spec/lti-dl/v2p0#workflow

    .. _LTI 1.3 Advantage Tool implementation in Python - LTI Message Launches:
        https://github.com/dmitry-viskov/pylti1.3?tab=readme-ov-file#lti-message-launches

    """

    def post(
        self,
        request: HttpRequest,
    ) -> Union[HttpResponse, LoggedHttpResponseBadRequest]:
        """HTTP POST request method.

        Validate LtiDeepLinkingRequest message and redirect to DeepLinkingFormView.

        Args:
            request: HttpRequest object.

        Returns:
            HttpResponse or LoggedHttpResponseBadRequest.

        """
        try:
            # Get launch message.
            message = self.get_message(request)
            # Check launch message type.
            validate_deep_linking_message(message)
            # Redirect to DeepLinkingForm view.
            return redirect(
                f'{app_config.name}:1.3:deep-linking:form',
                launch_id=message.get_launch_id().replace('lti1p3-launch-', ''),
            )
        except (LtiException, DeepLinkingException) as exc:
            return self.http_response_error(exc)


# NOTE: This view requires to be exempted from the protection of the
# CSRF and X-Frame-Options middlewares, as it is intended to be embedded
# in an iframe within the platform. The view is protected by the LTI
# authentication and authorization mechanisms.
@method_decorator([csrf_exempt, xframe_options_exempt], name='dispatch')
class DeepLinkingFormView(LTIToolView):
    """Deep Linking Form View.

    This view renders an interface allowing the user to discover and select one
    or more specific items to integrate back into the platform and also redirect
    the user's browser back to the platform along with details of the item(s) selected.

    Attributes:
        form_class (DeepLinkingForm): View Form class.

    .. _LTI Deep Linking Specification - Workflow:
        https://www.imsglobal.org/spec/lti-dl/v2p0#workflow

    .. _LTI 1.3 Advantage Tool implementation in Python - LTI Message Launches:
        https://github.com/dmitry-viskov/pylti1.3?tab=readme-ov-file#lti-message-launches

    """

    form_class = DeepLinkingForm

    def get(
        self,
        request: HttpRequest,
        launch_id: uuid4,
    ) -> Union[HttpResponse, LoggedHttpResponseBadRequest]:
        """HTTP GET request method.

        Validate cached LtiDeepLinkingRequest message and render DeepLinkingForm.

        Args:
            request: HttpRequest object.
            launch_id: Launch ID UUID4.

        Returns:
            HttpResponse or LoggedHttpResponseBadRequest.

        """
        try:
            # Get message from cache.
            message = self.get_message_from_cache(request, launch_id)
            # Validate message.
            validate_deep_linking_message(message)
            # Render form template. When the launch resolves to a single course, the
            # picker is scoped to that course's content instead of listing all courses.
            return render(
                request,
                configuration_helpers().get_value(
                    'OLTITP_DEEP_LINKING_FORM_TEMPLATE',
                    settings.OLTITP_DEEP_LINKING_FORM_TEMPLATE,
                ),
                {
                    'launch_id': launch_id,
                    'course_id': get_scoped_course_id(message.get_launch_data()),
                },
            )
        except (LtiException, DeepLinkingException) as exc:
            return self.http_response_error(exc)

    def post(
        self,
        request: HttpRequest,
        launch_id: uuid4,
    ) -> Union[HttpResponse, LoggedHttpResponseBadRequest]:
        """HTTP POST request method.

        Validate cached LtiDeepLinkingRequest message, DeepLinkingForm
        and render Deep Linking Response with selected form items.

        Args:
            request: HttpRequest object.
            launch_id: Launch ID UUID4.

        Returns:
            HttpResponse or LoggedHttpResponseBadRequest.

        """
        try:
            # Get message from cache.
            message = self.get_message_from_cache(request, launch_id)
            # Validate message.
            validate_deep_linking_message(message)
            # Initialize form.
            form = self.form_class(request.POST)
            # Validate form.
            if not form.is_valid():
                raise DeepLinkingException(form.errors)
            # Render Deep Linking response.
            return HttpResponse(
                message.get_deep_link().output_response_form(
                    form.cleaned_data.get('deep_link_resources', []),
                )
            )
        except (LtiException, DeepLinkingException) as exc:
            return self.http_response_error(exc)
