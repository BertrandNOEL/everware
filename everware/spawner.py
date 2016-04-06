import re
import pwd
import zipfile
from io import BytesIO
from tempfile import mkdtemp
from datetime import timedelta
from os.path import join as pjoin

from concurrent.futures import ThreadPoolExecutor

from docker.errors import APIError

from dockerspawner import DockerSpawner
from textwrap import dedent
from traitlets import (
    Integer,
    Unicode,
)
from tornado import gen
from tornado.ioloop import IOLoop

from escapism import escape

from .image_handler import ImageHandler

import ssl

import json

from .git_executor import GitExecutor

ssl._create_default_https_context = ssl._create_unverified_context

class CustomDockerSpawner(DockerSpawner):
    def __init__(self, **kwargs):
        self._user_log = []
        self._is_failed = False
        self._is_building = False
        self._image_handler = ImageHandler()
        self._cur_waiter = None
        self._git_executor = None
        super(CustomDockerSpawner, self).__init__(**kwargs)


    # We override the executor here to increase the number of threads
    @property
    def executor(self):
        """single global executor"""
        cls = self.__class__
        if cls._executor is None:
            cls._executor = ThreadPoolExecutor(20)
        return cls._executor


    def _docker(self, method, *args, **kwargs):
        """wrapper for calling docker methods

        to be passed to ThreadPoolExecutor
        """
        # methods that return a generator object return instantly
        # before the work they were meant to do is complete
        generator_methods = ('build',)
        m = getattr(self.client, method)

        if method in generator_methods:
            def lister(mm):
                ret = []
                for l in mm:
                    for lj in l.decode().split('\r\n'):
                        if len(lj) > 0:
                            ret.append(lj)
                            try:
                                j = json.loads(lj)
                            except json.JSONDecodeError as e:
                                self.log.warn("Error decoding string to json: %s" % lj)
                            else:
                                if 'stream' in j and not j['stream'].startswith(' --->'):
                                # self._add_to_log(l['stream'], 2)
                                    self._cur_waiter.add_to_log(j['stream'], 2)
                return ret
            return lister(m(*args, **kwargs))
        else:
            return m(*args, **kwargs)

    def clear_state(self):
        state = super(CustomDockerSpawner, self).clear_state()
        self.container_id = ''

    def _options_form_default(self):
        return """
            <label for="username_input">Git repository:</label>
            <input
              id="repository_input"
              type="text"
              autocapitalize="off"
              autocorrect="off"
              class="form-control"
              name="repository_url"
              tabindex="1"
              autofocus="autofocus"
            />
        """

    def options_from_form(self, formdata):
        options = {}
        options['repo_url'] = formdata.get('repository_url', [''])[0].strip()
        if not options['repo_url']:
            raise Exception('You have to provide the URL to a git repository.')

        return options

    @property
    def repo_url(self):
        return getattr(
            self._git_executor,
            'processed_repo_url',
        )

    @property
    def form_repo_url(self):
        return self.user_options.get('repo_url', '')

    @property
    def branch_name(self):
        return self._git_executor.branch_name

    @property
    def commit_sha(self):
        return self._git_executor.repo_sha

    @property
    def container_name(self):
        return "{}-{}".format(self.container_prefix,
                              self.escaped_name)

    @gen.coroutine
    def get_container(self):

        self.log.debug("Getting container: %s", self.container_name)
        try:
            container = yield self.docker(
                'inspect_container', self.container_name
            )
            self.container_id = container['Id']
        except APIError as e:
            if e.response.status_code == 404:
                self.log.info("Container '%s' is gone", self.container_name)
                container = None
                # my container is gone, forget my id
                self.container_id = ''
            else:
                raise
        return container

    @gen.coroutine
    def get_image(self, image_name):
        images = yield self.docker('images')
        if ':' in image_name:
            tag_processor = lambda tag: tag
        else:
            tag_processor = lambda tag: tag.split(':')[0]
        for img in images:
            tags = (tag_processor(tag) for tag in img['RepoTags'])
            if image_name in tags:
                return img

    @property
    def user_log(self):
        if self._is_building:
            build_log = getattr(self._cur_waiter, 'building_log', [])
            return self._user_log + build_log
        else:
            return self._user_log

    @property
    def is_failed(self):
        return self._is_failed

    def _add_to_log(self, message, level=1):
        self._user_log.append({
            'text': message,
            'level': level
        })

    @gen.coroutine
    def build_image(self):
        """download the repo and build a docker image if needed"""
        if self.form_repo_url.startswith('docker:'):
            image_name = self.form_repo_url.replace('docker:', '')
            image = yield self.get_image(image_name)
            if image is None:
                raise Exception('Image %s doesn\'t exist' % image_name)
            else:
                self._add_to_log('Image %s is found' % image_name)
                return image_name

        tmp_dir = mkdtemp(suffix='-everware')
        self._git_executor = GitExecutor(self.form_repo_url, tmp_dir)
        self._add_to_log('Cloning repository %s' % self.repo_url)
        self.log.info('Cloning repo %s' % self.repo_url)
        yield self._git_executor.exec()
        # use git repo URL and HEAD commit sha to derive
        # the image name

        image_name = "everware/{}-{}".format(
            self._git_executor.escaped_repo_url,
            self._git_executor.repo_sha
        )

        self._add_to_log('Building image (%s)' % image_name)

        with self._image_handler.get_waiter(image_name) as self._cur_waiter:
            if self._cur_waiter.last_exception:
                raise self._cur_waiter.last_exception
            yield self._cur_waiter.block()
            last_exception = self._cur_waiter.last_exception
            if last_exception is not None:
                raise last_exception
            image = yield self.get_image(image_name)
            if image is not None:
                return image_name
            self.log.debug("Building image {}".format(image_name))
            build_log = yield self.docker(
                'build',
                path=tmp_dir,
                tag=image_name,
                rm=True,
            )
            self._user_log.extend(self._cur_waiter.building_log)
            full_output = "".join(str(line) for line in build_log)
            self.log.debug(full_output)
            image = yield self.get_image(image_name)
            if image is None:
                raise Exception(full_output)

        return image_name

    @gen.coroutine
    def start(self, image=None):
        """start the single-user server in a docker container"""
        self._user_log = []
        self._is_failed = False
        self._is_building = True
        try:
            f = self.build_image()
            image_name = yield gen.with_timeout(
                timedelta(seconds=self.start_timeout),
                f
            )
            self._is_building = False
            self.log.info("Starting container from image: %s" % image_name)
            self._add_to_log('Creating container')
            yield super(CustomDockerSpawner, self).start(
                image=image_name
            )
            self._add_to_log('Adding to proxy')
        except gen.TimeoutError:
            self._is_failed = True
            if self._cur_waiter:
                self._user_log.extend(self._cur_waiter.building_log)
                self._cur_waiter.timeout_happened()
            self._is_building = False
            self._add_to_log(
                'Building took too long (> %.3f secs)' % self.start_timeout,
                level=2
            )
            raise
        except Exception as e:
            self._is_failed = True
            message = str(e)
            if message.startswith('Failed to get port'):
                message = "Container doesn't have jupyter-singleuser inside"
            elif 'Cannot locate specified Dockerfile' in message:
                message = "Your repo doesn't include Dockerfile"
            self._add_to_log('Something went wrong during building. Error: %s' % message)
            raise e

    @gen.coroutine
    def is_running(self):
        status = yield self.poll()
        return status is None

    def get_env(self):
        env = super(CustomDockerSpawner, self).get_env()
        env.update({
            'JPY_GITHUBURL': self._git_executor.processed_repo_url,
            'JPY_REPOPOINTER': self._git_executor.repo_sha,
            'JPY_WORKDIR': '/notebooks'
        })
        return env


class CustomSwarmSpawner(CustomDockerSpawner):
    container_ip = '0.0.0.0'
    #start_timeout = 42 #180

    def __init__(self, **kwargs):
        super(CustomSwarmSpawner, self).__init__(**kwargs)

    @gen.coroutine
    def lookup_node_name(self):
        """Find the name of the swarm node that the container is running on."""
        containers = yield self.docker('containers', all=True)
        for container in containers:
            if container['Id'] == self.container_id:
                name, = container['Names']
                node, container_name = name.lstrip("/").split("/")
                raise gen.Return(node)

    @gen.coroutine
    def start(self, image=None, extra_create_kwargs=None):
        yield super(CustomSwarmSpawner, self).start(
            image=image
        )

        container = yield self.get_container()
        if container is not None:
            node_name = container['Node']['Name']
            self.user.server.ip = node_name
            self.db.commit()
            self.log.info("{} was started on {} ({}:{})".format(
                self.container_name, node_name, self.user.server.ip, self.user.server.port))
