# (C) Datadog, Inc. 2019-present
# All rights reserved
# Licensed under Simplified BSD License (see LICENSE)
import json
from collections import defaultdict

from pyVmomi import vim

from datadog_checks.base.utils.http import RequestsWrapper

from .api import APIConnectionError, APIResponseError, smart_retry

MOR_TYPE_MAPPING = {
    'HostSystem': vim.HostSystem,
    'VirtualMachine': vim.VirtualMachine,
    'Datacenter': vim.Datacenter,
    'Datastore': vim.Datastore,
    'ClusterComputeResource': vim.ClusterComputeResource,
}

JSON_HEADERS = {'Content-Type': 'application/json'}


class VSphereRestAPI(object):
    """
    Abstraction class over the vSphere REST api using the vsphere-automation-sdk-python library
    """

    def __init__(self, config, log):
        self.config = config
        self.log = log

        config = {
            'username': self.config.username,
            'password': self.config.password,
            'tls_ca_cert': self.config.ssl_capath,
            'tls_verify': self.config.ssl_verify,
        }
        self._http = RequestsWrapper(config, {})
        self._api_base_url = "https://{}/rest/com/vmware/cis/".format(self.config.hostname)

        self.smart_connect()

    def smart_connect(self):
        """
        Create a vSphere client.
        """
        try:
            resp = self._http.post(self._api_base_url + "session")
            resp.raise_for_status()
        except Exception as e:
            err_msg = "Connection to vSphere Rest API failed for host {}: {}".format(self.config.hostname, e)
            raise APIConnectionError(err_msg)

        session_token = resp.json().get('value')
        if not session_token:
            raise APIConnectionError("Failed to retrieve session token")

        self._http.options['headers']['vmware-api-session-id'] = session_token

    @smart_retry
    def get_resource_tags(self):
        """
        Get resource tags.

        Response structure:

            {
                <RESOURCE_TYPE>: {
                    <RESOURCE_MOR_ID>: ['<CATEGORY_NAME>:<TAG_NAME>', ...]
                },
                ...
            }
        """
        categories = self._get_categories()
        tags = self._get_tags(categories)
        tag_ids = list(tags.keys())
        tag_associations = self._get_tag_associations(tag_ids)
        self.log.debug("Fetched tag associations: %s", tag_associations)

        # Initialise resource_tags
        resource_tags = {resource_type: defaultdict(list) for resource_type in MOR_TYPE_MAPPING.values()}

        for tag_asso in tag_associations:
            tag = tags[tag_asso['tag_id']]
            for resource_asso in tag_asso['object_ids']:
                resource_type = MOR_TYPE_MAPPING.get(resource_asso['type'])
                if not resource_type:
                    continue
                resource_tags[resource_type][resource_asso['id']].append(tag)
        self.log.debug("Result resource tags: %s", resource_tags)
        return resource_tags

    def _get_tag_associations(self, tag_ids):
        """
        :return: tag_associations: the structure of the tag associations is as follow:
            [
                {
                    "object_ids": [
                        {"id": "vm-745", "type": "VirtualMachine"},
                        {"id": "group-d1", "type": "Folder"},
                        ...
                    ],
                    "tag_id": "urn:vmomi:InventoryServiceTag:da5e9cde-524c-4971-8d6e-4815ee1e8dda:GLOBAL"
                },
                ...
            ]
        """
        payload = {"tag_ids": tag_ids}
        tag_associations = self._request_json(
            "tagging/tag-association?~action=list-attached-objects-on-tags",
            method="post",
            data=json.dumps(payload),
            extra_headers=JSON_HEADERS,
        )
        return tag_associations

    def _get_categories(self):
        """
        Returns a dict of categories with category id as key and category name as value.

        :return: categories: the structure of the categories is as follow:
            {
                <CATEGORY_ID>: <CATEGORY_NAME>,
                <CATEGORY_ID>: <CATEGORY_NAME>,
                ...
            }
        """
        category_ids = self._request_json("tagging/category")
        categories = {}
        for category_id in category_ids:
            cat = self._request_json("tagging/category/id:{}".format(category_id))
            categories[category_id] = cat['name']
        return categories

    def _get_tags(self, categories):
        """
        Create tags using vSphere tags prefix + vSphere tag category name as key and vSphere tag name as value.

            <VSPHERE_TAGS_PREFIX><TAG_CATEGORY>:<TAG_NAME>

        Examples:
            - os_type:windows
            - application_name:my_app

        Taging best practices:
        https://www.vmware.com/content/dam/digitalmarketing/vmware/en/pdf/techpaper/performance/tagging-vsphere67-perf.pdf
        """
        tag_ids = self._request_json("tagging/tag")
        tags = {}
        for tag_id in tag_ids:
            tag = self._request_json("tagging/tag/id:{}".format(tag_id))
            cat_name = categories.get(tag['category_id'], 'unknown_category')
            tags[tag_id] = "{}{}:{}".format(self.config.vsphere_tags_prefix, cat_name, tag['name'])
        return tags

    def _request_json(self, endpoint, method='get', **options):
        url = self._api_base_url + endpoint
        resp = getattr(self._http, method)(url, **options)
        resp.raise_for_status()

        data = resp.json()
        if 'value' not in data:
            raise APIResponseError("Missing `value` element in response for url: {}".format(url))

        return data['value']
