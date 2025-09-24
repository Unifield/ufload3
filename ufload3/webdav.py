# -*- coding: utf-8 -*-

import requests
from office365.sharepoint.client_context import ClientContext
from office365.runtime.client_runtime_context import ClientRuntimeContext
from office365.runtime.client_request_exception import ClientRequestException
from office365.sharepoint.files.file import File
from office365.sharepoint.folders.folder import Folder
from office365.runtime.queries.service_operation import ServiceOperationQuery
from cryptography.x509 import load_pem_x509_certificate
from cryptography.hazmat.primitives import hashes

def move_to_newname(self, destination, newname, flag):
    """Moves the file to the specified destination url.

    :param str or office365.sharepoint.folders.folder.Folder destination: Specifies the existing folder or folder
         site relative url
    :param str new file name
    :param int flag: Specifies the kind of move operation.
    """

    def _moveto(destination_folder):
        # type: (Folder) -> None
        file_url = "/".join([str(destination_folder.serverRelativeUrl), newname])

        params = {"newurl": file_url, "flags": flag}
        qry = ServiceOperationQuery(self, "moveto", params)
        self.context.add_query(qry)

        def _update_file(return_type):
            self.set_property("ServerRelativeUrl", file_url)

        self.context.after_query_execute(_update_file)

    def _source_file_resolved():
        if isinstance(destination, Folder):
            destination.ensure_property("ServerRelativeUrl", _moveto, destination)
        else:
            self.context.web.ensure_folder_path(destination).get().after_execute(
                _moveto
            )

    self.ensure_properties(["ServerRelativeUrl", "Name"], _source_file_resolved)
    return self

File.move_to_newname = move_to_newname

import logging
import os
import posixpath
from urllib.parse import urlparse, urljoin
from time import sleep

def execute_query_retry(
    self,
    max_retry=5,
    timeout_secs=5,
    success_callback=None,
    failure_callback=None,
    exceptions=(ClientRequestException,),
):
    """
    Executes the current set of data retrieval queries and method invocations and retries it if needed.

    :param int max_retry: Number of times to retry the request
    :param int timeout_secs: Seconds to wait before retrying the request.
    :param (office365.runtime.client_object.ClientObject)-> None success_callback:
    :param (int, requests.exceptions.RequestException)-> None failure_callback:
    :param exceptions: tuple of exceptions that we retry
    """
    retry = 1
    while True:
        try:
            self.execute_query()
            if callable(success_callback):
                success_callback(self.current_query.return_type)
            break
        except exceptions as e:
            if retry > max_retry:
                raise
            retry += 1
            self.add_query(self.current_query)
            if callable(failure_callback):
                failure_callback(retry, e)
            sleep(timeout_secs)
ClientRuntimeContext.execute_query_retry=execute_query_retry


class ConnectionFailed(Exception):
    pass

class PasswordFailed(Exception):
    pass

class Client(object):
    def __init__(self, host, tenant, client_id, cert_content=False, cert_path=False, max_retry=1, path=None):
        self.tenant = tenant
        self.client_id = client_id
        if not cert_content and cert_path:
            with open(cert_path, 'r') as c:
                cert_content = c.read()
        self.cert_content = cert_content
        cert = load_pem_x509_certificate(bytes(self.cert_content, 'utf8'))
        self.thumbprint = cert.fingerprint(hashes.SHA1()).hex()

        self.path = path or ''

        self.url = 'https://{0}'.format(host)

        self.max_retry = max_retry
        self.login()

    def login(self):
        full_path = urljoin(self.url, self.path)
        self.request = ClientContext(full_path)

        self.request.with_client_certificate(
            tenant=self.tenant,
            client_id=self.client_id,
            private_key=self.cert_content,
            thumbprint=self.thumbprint,
        )
        self.baseurl = self.request._get_context_web_information().WebFullUrl
        self.request._auth_context.url = self.baseurl
        if not self.baseurl:
            raise requests.exceptions.RequestException("Full Url not found %s" % self.path)
        if not self.baseurl.endswith('/'):
            self.baseurl = '%s/' % self.baseurl
        self.path = urlparse(full_path).path

        if not self.path.startswith('/'):
            self.path = '/%s' % self.path
        if not self.path.endswith('/') and len(self.path) > 1:
            self.path = '%s/' % (self.path, )

    def create_folder(self, remote_path):
        if not self.folder_exists(remote_path):
            self.request.web.get_folder_by_server_relative_url(self.path).add(remote_path).execute_query_with_incremental_retry(max_retry=self.max_retry)
        return True

    def delete(self, remote_path):
        webUri = '%s%s' % (self.path, remote_path)
        return self.request.web.get_file_by_server_relative_url(webUri).delete_object().execute_query_with_incremental_retry(max_retry=self.max_retry)

    def move(self, remote_path, dest, retry=True):
        # Move file to folder
        webUri = '%s%s' % (self.path, remote_path)

        to_folder_dest = posixpath.join(self.path, dest)
        self.request.web.get_file_by_server_relative_path(webUri).moveto(to_folder_dest, 1).execute_query_with_incremental_retry(max_retry=self.max_retry)
        return True

    def move_to_file(self, remote_path, dest):
        webUri = '%s%s' % (self.path, remote_path)
        full_name_dest = '/'.join([self.path, dest])

        dest_file_name = full_name_dest.split('/')[-1]
        dest_folder = '/'.join(full_name_dest.split('/')[0:-1])
        self.request.web.get_file_by_server_relative_path(webUri).move_to_newname(dest_folder, dest_file_name, 1).execute_query_with_incremental_retry(max_retry=self.max_retry)
        return True

    def upload(self, fileobj, remote_path, buffer_size=None, log=False, progress_obj=False, continuation=False):
        split_name = remote_path.split('/')
        new_file = split_name.pop()
        split_name.insert(0, self.path)
        path  = '/'.join(split_name)
        if path[-1] != '/':
            path += '/'

        if buffer_size is None:
            buffer_size = 10 * 1024 * 1024

        if progress_obj:
            log = True

        report_method = None
        def report_progress(uploaded, **a):
            percent_txt = ''
            if size:
                percent = round(uploaded*100/size)
                percent_txt = '%d%%' % percent
                if progress_obj:
                    progress_obj.write({'name': percent})

            if logger:
                logger.info('OneDrive: %d bytes sent on %s bytes %s' % (uploaded, size or 'unknown', percent_txt))

        if log:
            logger = logging.getLogger('cloud.backup')
            try:
                size = os.path.getsize(fileobj.name)
            except:
                size = None
            report_method = report_progress

        target_folder = self.request.web.get_folder_by_server_relative_url(path)
        return target_folder.files.create_upload_session(
            fileobj, buffer_size, chunk_uploaded=report_method, file_name=new_file
        ).execute_query_with_incremental_retry(max_retry=self.max_retry)

    def list(self, remote_path):
        if not remote_path.startswith(self.path):
            remote_path = posixpath.join(self.path, remote_path)
        return (
            self.request.web.get_folder_by_server_relative_path(remote_path)
            .get_files()
            .expand(["TimeLastModified"])
            .execute_query_retry(max_retry=self.max_retry)
        )

    def folder_exists(self, remote_path):
        webUri = '%s%s' % (self.path, remote_path)
        try:
            self.request.web.get_folder_by_server_relative_url(webUri).get().execute_query_with_incremental_retry(max_retry=self.max_retry)
        except ClientRequestException as e:
            if e.response.status_code == 404:
                return False
            raise
        return True

    def download(self, remote_path, filename):
        if not remote_path.startswith(self.path):
            remote_path = posixpath.join(self.path, remote_path)

        src = self.request.web.get_file_by_server_relative_path(remote_path)
        with open(filename, 'wb') as file:
            src.download_session(file).execute_query_with_incremental_retry(max_retry=self.max_retry)
        return filename

