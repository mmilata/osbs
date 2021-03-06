from __future__ import print_function, unicode_literals, absolute_import
import json

import logging
import time
from osbs.constants import POD_FINISHED_STATES

try:
    # py2
    import httplib
    import urlparse
except ImportError:
    # py3
    import http.client as httplib
    import urllib.parse as urlparse

from .http import get_http_session


logger = logging.getLogger(__name__)


class OpenshiftException(Exception):
    """ OpenShift didn't respond with OK (200) status """

    def __init__(self, status_code, *args, **kwargs):
        super(OpenshiftException, self).__init__(*args, **kwargs)
        self.status_code = status_code


def check_response(response):
    if response.status_code not in (httplib.OK, httplib.CREATED):
        raise OpenshiftException(response.status_code)


# TODO: error handling: create function which handles errors in response object
class Openshift(object):

    def __init__(self, openshift_api_url, openshift_oauth_url, kubelet_base, verbose=False,
                 username=None, password=None, use_kerberos=False, verify_ssl=True):
        self.os_api_url = openshift_api_url
        self._os_oauth_url = openshift_oauth_url
        self.kubelet_base = kubelet_base
        self.verbose = verbose
        self.verify_ssl = verify_ssl
        self._con = get_http_session(verbose=self.verbose)

        # auth stuff
        self.use_kerberos = use_kerberos
        self.username = username
        self.password = password
        self.use_auth = bool(use_kerberos or (username and password))
        self.token = None

    @property
    def os_oauth_url(self):
        return self._os_oauth_url

    def _build_url(self, url):
        return urlparse.urljoin(self.os_api_url, url)

    def _build_k8s_url(self, url):
        return urlparse.urljoin(self.kubelet_base, url)

    def _request_args(self, with_auth=True, **kwargs):
        headers = kwargs.pop("headers", {})
        if with_auth and self.use_auth:
            # TODO: this setup is not covered:
            # httpd being used as an basic auth proxy: you need to pass Authorization to httpd
            # and at the same time authenticate within openshift, therefore we would have to
            # append token to URL, and set Authorization for httpd's basic auth
            if self.token is None:
                self.get_oauth_token()
            headers["Authorization"] = "Bearer %s" % self.token
        return headers, kwargs

    def _post(self, url, with_auth=True, **kwargs):
        headers, kwargs = self._request_args(with_auth, **kwargs)
        return self._con.get(url, headers=headers, verify_ssl=self.verify_ssl, **kwargs)

    def _get(self, url, with_auth=True, **kwargs):
        headers, kwargs = self._request_args(with_auth, **kwargs)
        return self._con.get(url, headers=headers, verify_ssl=self.verify_ssl, **kwargs)

    def get_oauth_token(self):
        url = self.os_oauth_url + "?response_type=token&client_id=openshift-challenging-client"
        if self.username and self.password:
            r = self._get(url, with_auth=False, allow_redirects=False, username=self.username, password=self.password)
        elif self.use_kerberos:
            r = self._get(url, with_auth=False, allow_redirects=False, kerberos_auth=True)
        else:
            raise ValueError("You need to specify an authentication type!")
        redir_url = r.headers['location']
        parsed_url = urlparse.urlparse(redir_url)
        fragment = parsed_url.fragment
        parsed_fragment = urlparse.parse_qs(fragment)
        self.token = parsed_fragment['access_token'][0]
        return self.token

    def create_build(self, build_json):
        """
        :return:
        """
        url = self._build_url("builds/")
        return self._post(url, data=build_json,
                          headers={"Content-Type": "application/json"})

    def get_build_config(self, build_config_id):
        url = self._build_url("buildConfigs/%s/" % build_config_id)
        response = self._get(url)
        build_config = response.json()
        return build_config

    def create_build_config(self, build_config_json):
        """
        :return:
        """
        url = self._build_url("buildConfigs/")
        return self._post(url, data=build_config_json,
                          headers={"Content-Type": "application/json"})

    def start_build(self, build_config_id):
        """
        :return:
        """
        build_config = self.get_build_config(build_config_id)
        assert build_config["Kind"] == "BuildConfig"
        build_config["Kind"] = "Build"
        return self.create_build(build_config)

    def logs(self, build_id, follow=False):
        """

        :param follow:
        :return:
        """
        redir_url = self._build_url("redirect/buildLogs/%s" % build_id)
        bl = self._get(redir_url, allow_redirects=False)
        attempts = 10
        while bl.status_code not in (httplib.OK, httplib.TEMPORARY_REDIRECT, httplib.MOVED_PERMANENTLY):
            bl = self._get(redir_url, allow_redirects=False)
            time.sleep(5)  # 50 seconds got to be enough
            if attempts <= 0:
                break
            attempts -= 1
        buildlogs_url = self._build_k8s_url(
            "containerLogs/default/build-%s/custom-build?follow=%d" % (
                build_id, 1 if follow else 0))
        time.sleep(15)  # container ***STILL*** may not be ready
        response = self._get(buildlogs_url, stream=follow, headers={'Connection': 'close'})
        if follow:
            return response.iter_lines()
        return response.content

    def list_builds(self):
        """

        :return:
        """
        url = self._build_url("builds")
        return self._get(url)

    def get_build(self, build_id):
        """

        :return:
        """
        url = self._build_url("builds/%s/" % build_id)
        response = self._get(url)
        check_response(response)
        return response

    def wait(self, build_id):
        """
        :param build_id: wait for build to finish

        :return:
        """
        logger.info("watching build '%s'", build_id)
        url = self._build_url("watch/builds/%s/" % build_id)
        response = self._get(url, stream=True, headers={'Connection': 'close'})
        for line in response.iter_lines():
            j = json.loads(line)
            logger.debug("got object change: '%s'", j['type'])
            if j['object']['status'].lower() in POD_FINISHED_STATES:
                logger.info("build has finished")
                return j['object']
        check_response(response)
        return response


if __name__ == '__main__':
    o = Openshift(openshift_api_url="https://localhost:8443/osapi/v1beta1/",
                  openshift_oauth_url="https://localhost:8443/oauth/authorize",
                  kubelet_base=None, verbose=True)
    print(o.get_oauth_token())
