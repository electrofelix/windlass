#
# (c) Copyright 2017 Hewlett Packard Enterprise Development LP
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
#

from docker import from_env
import windlass.api
import windlass.tools
from git import Repo
import logging
import os

import yaml


def check_docker_stream(stream, name):
    # Read output from docker command and raise exception
    # if docker hit an error processing the command.
    # Also log messages if debugging is turned on.
    last_msgs = []
    for line in stream:
        if not line:
            continue

        data = yaml.load(line)
        if 'status' in data:
            if 'id' in data:
                msg = '%s layer %s: %s' % (name,
                                           data['id'],
                                           data['status'])
            else:
                msg = '%s: %s' % (name, data['status'])
            if msg not in last_msgs:
                logging.debug(msg)
                last_msgs.append(msg)
        if 'error' in data:
            logging.error("Error processing image %s:%s" % (
                name, "\n".join(last_msgs)))
            raise windlass.api.RetryableFailure('%s ERROR from docker: %s' % (
                name, data['error']))


def push_image(name, imagename, push_tag='latest', auth_config=None):
    docker = from_env(version='auto')
    logging.info('%s: Pushing as %s:%s', name, imagename, push_tag)

    # raises exception if imagename is missing
    docker.images.get(imagename + ':' + push_tag)

    output = docker.images.push(
        imagename, push_tag, auth_config=auth_config,
        stream=True)
    check_docker_stream(output, name)
    return True


def clean_tag(tag):
    clean = ''
    valid = ['_', '-', '.']
    for c in tag:
        if c.isalnum() or c in valid:
            clean += c
        else:
            clean += '_'
    return clean[:128]


def build_verbosly(name, path, nocache=False, dockerfile=None,
                   pull=False):
    docker = from_env(version='auto')
    bargs = windlass.tools.load_proxy()
    logging.info("Building %s from path %s", name, path)
    stream = docker.api.build(path=path,
                              tag=name,
                              nocache=nocache,
                              buildargs=bargs,
                              dockerfile=dockerfile,
                              stream=True,
                              pull=pull)
    errors = []
    output = []
    for line in stream:
        data = yaml.load(line.decode())
        if 'stream' in data:
            for out in data['stream'].split('\n\r'):
                logging.debug('%s: %s', name, out.strip())
                # capture detailed output in case of error
                output.append(out)
        elif 'error' in data:
            errors.append(data['error'])
    if errors:
        logging.error('Failed to build %s:\n%s', name, '\n'.join(errors))
        logging.error('Output from building %s:\n%s', name, ''.join(output))
        raise Exception("Failed to build {}".format(name))
    logging.info("Successfully built %s from path %s", name, path)
    return docker.images.get(name)


def build_image_from_local_repo(repopath, imagepath, name, tags=[],
                                nocache=False, dockerfile=None, pull=False):
    logging.info('%s: Building image from local directory %s',
                 name, os.path.join(repopath, imagepath))
    repo = Repo(repopath)
    image = build_verbosly(name, os.path.join(repopath, imagepath),
                           nocache=nocache, dockerfile=dockerfile)
    if repo.head.is_detached:
        commit = repo.head.commit.hexsha
    else:
        commit = repo.active_branch.commit.hexsha
        image.tag(name,
                  clean_tag('branch_' +
                            repo.active_branch.name.replace('/', '_')))
    if repo.is_dirty():
        image.tag(name,
                  clean_tag('last_ref_' + commit))
    else:
        image.tag(name, clean_tag('ref_' + commit))

    return image


@windlass.api.register_type('images')
class Image(windlass.api.Artifact):

    def __init__(self, data):
        super().__init__(data)
        self.name, devtag = windlass.tools.split_image(data['name'])
        if not self.version:
            self.version = devtag

        self.client = from_env(version='auto')

    def pull_image(self, remoteimage, imagename, tag):
        """Pull the remoteimage down

        And tag it with the imagename and tag.
        """
        logging.info("%s: Pulling image from %s", imagename, remoteimage)

        output = self.client.api.pull(remoteimage, stream=True)
        check_docker_stream(output, imagename)
        self.client.api.tag(remoteimage, imagename, tag)

        image = self.client.images.get('%s:%s' % (imagename, tag))
        return image

    def url(self, version=None, docker_image_registry=None):
        if version is None:
            version = self.version

        if docker_image_registry:
            return '%s/%s:%s' % (
                docker_image_registry.rstrip('/'), self.name, version)
        return '%s:%s' % (self.name, version)

    def build(self):
        # How to pass in no-docker-cache and docker-pull arguments.
        image_def = self.data

        if 'remote' in image_def:
            self.pull_image(
                image_def['remote'],
                *windlass.tools.split_image(image_def['name']))
        else:
            # TODO(kerrin) - repo should be relative the defining yaml file
            # and not the current working directory of the program. This change
            # is likely to break this.
            repopath = self.metadata['repopath']

            dockerfile = image_def.get('dockerfile', None)
            logging.debug('Expecting repository at %s' % repopath)
            build_image_from_local_repo(repopath,
                                        image_def['context'],
                                        image_def['name'],
                                        nocache=False,
                                        dockerfile=dockerfile,
                                        pull=True)
            logging.info('Get image %s completed', image_def['name'])

    @windlass.api.retry()
    @windlass.api.fall_back('docker_image_registry')
    def download(self, version=None, docker_image_registry=None, **kwargs):
        if version is None and self.version is None:
            raise Exception('Must specify version of image to download.')

        if docker_image_registry is None:
            raise Exception(
                'docker_image_registry not set for image download. '
                'Where should we download from?')

        tag = version or self.version

        logging.info('Pinning image: %s to pin: %s' % (self.name, tag))
        remoteimage = '%s/%s:%s' % (docker_image_registry, self.name, tag)

        # Pull the remoteimage down and tag it with the name of artifact
        # and the requested version
        self.pull_image(remoteimage, self.name, tag)

        if tag != self.version:
            # Tag the image with the version but without the repository
            self.client.api.tag(remoteimage, self.name, self.version)

    @windlass.api.retry()
    @windlass.api.fall_back('docker_image_registry', first_only=True)
    def upload(self, version=None, docker_image_registry=None,
               docker_user=None, docker_password=None,
               **kwargs):
        if docker_image_registry is None:
            raise Exception(
                'docker_image_registry not set for image upload. '
                'Unable to publish')

        if docker_user:
            auth_config = {
                'username': docker_user,
                'password': docker_password}
        else:
            auth_config = None

        # Local image name on the node
        local_fullname = self.url(self.version)

        # Upload image with this tag
        upload_tag = version or self.version
        upload_name = '%s/%s' % (docker_image_registry.rstrip('/'), self.name)
        fullname = '%s:%s' % (upload_name, upload_tag)

        try:
            if docker_image_registry:
                self.client.api.tag(local_fullname, upload_name, upload_tag)
            push_image(
                self.name, upload_name, upload_tag, auth_config=auth_config)
        finally:
            if docker_image_registry:
                self.client.api.remove_image(fullname)

        logging.info('%s: Successfully pushed', self.name)
