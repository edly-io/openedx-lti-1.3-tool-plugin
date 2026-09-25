"""Django Forms."""
import logging

from django import forms
from django.utils.translation import gettext as _
from pylti1p3.deep_link_resource import DeepLinkResource

from openedx_lti_tool_plugin.validators import JSONSchemaValidator

log = logging.getLogger(__name__)


class DeepLinkingForm(forms.Form):
    """Deep Linking Form."""

    CONTENT_ITEMS_SCHEMA = {
        'type': 'array',
        'items': {
            'type': 'object',
            'properties': {
                'type': {'type': 'string'},
                'url': {'type': 'string'},
                'title': {'type': 'string'},
                'custom': {
                    'type': 'object',
                    'properties': {
                        'resourceId': {'type': 'string'},
                    },
                },
            },
            'additionalProperties': True,
        },
    }

    content_items = forms.JSONField(
        required=False,
        validators=[JSONSchemaValidator(CONTENT_ITEMS_SCHEMA)],
    )

    def __init__(self, *args, accept_multiple: bool = False, **kwargs):
        """Initialize the form.

        Args:
            *args: Variable length argument list.
            accept_multiple: Whether the platform accepts more than one content item,
                as advertised by the Deep Linking Settings claim. Defaults to False,
                the behavior expected when the platform does not send the property.
            **kwargs: Arbitrary keyword arguments.

        """
        super().__init__(*args, **kwargs)
        self.accept_multiple = accept_multiple

    def clean(self) -> dict:
        """Form clean.

        This method will transform all the dictionaries from the cleaned content_items data
        into a list of DeepLinkResource objects that will be added to the cleaned data
        dictionary deep_link_resources key.

        Returns:
            A dictionary with cleaned form data.

        .. _LTI 1.3 Advantage Tool implementation in Python - LTI Message Launches:
            https://github.com/dmitry-viskov/pylti1.3?tab=readme-ov-file#deep-linking-responses

        """
        super().clean()
        deep_link_resources = []
        content_items = self.cleaned_data.get('content_items') or []

        # The platform told us it only accepts one content item, so returning several
        # would hand it content it did not ask for. The picker already enforces this,
        # but the POST is not trustworthy on its own.
        if not self.accept_multiple and len(content_items) > 1:
            raise forms.ValidationError(
                _('This platform only accepts one content item per selection.'),
                code='multiple_content_items',
            )

        for content_item in content_items:
            deep_link_resource = DeepLinkResource()
            deep_link_resource.set_type(content_item.get('type', ''))
            deep_link_resource.set_title(content_item.get('title', ''))
            deep_link_resource.set_url(content_item.get('url', ''))
            deep_link_resource.set_custom_params(content_item.get('custom', {}))
            deep_link_resources.append(deep_link_resource)

        self.cleaned_data['deep_link_resources'] = deep_link_resources

        return self.cleaned_data
