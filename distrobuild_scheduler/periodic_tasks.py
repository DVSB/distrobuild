#  Copyright (c) 2021 The Distrobuild Authors
#
#  Permission is hereby granted, free of charge, to any person obtaining a copy
#  of this software and associated documentation files (the "Software"), to deal
#  in the Software without restriction, including without limitation the rights
#  to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
#  copies of the Software, and to permit persons to whom the Software is
#  furnished to do so, subject to the following conditions:
#
#  The above copyright notice and this permission notice shall be included in all
#  copies or substantial portions of the Software.
#
#  THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
#  IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
#  FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
#  AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
#  LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
#  OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
#  SOFTWARE.

import asyncio
import datetime
import xmlrpc
from typing import List

import koji
from tortoise.transactions import atomic

from distrobuild.common import tags
from distrobuild.models import Build, BuildStatus, Package
from distrobuild.session import koji_session, mbs_client
from distrobuild.settings import settings

from distrobuild_scheduler import logger
from distrobuild_scheduler.sigul import sign_koji_package


@atomic()
async def atomic_sign_unsigned_builds(build: Build):
    if build.koji_id:
        koji_session.packageListAdd(tags.compose(), build.package.name, "distrobuild")

        should_tag = True
        build_tasks = koji_session.listBuilds(taskID=build.koji_id)
        for build_task in build_tasks:
            build_history = koji_session.queryHistory(build=build_task["build_id"])
            if "tag_listing" in build_history:
                for tag in build_history["tag_listing"]:
                    if tag["tag.name"] == tags.compose():
                        should_tag = False

            if should_tag:
                koji_session.tagBuild(tags.compose(), build_task["nvr"])

            build_rpms = koji_session.listBuildRPMs(build_task["build_id"])
            for rpm in build_rpms:
                rpm_sigs = koji_session.queryRPMSigs(rpm["id"])
                for rpm_sig in rpm_sigs:
                    if rpm_sig["sigkey"] == settings.sigul_key_id:
                        continue

                nvr_arch = "%s.%s" % (rpm["nvr"], rpm["arch"])
                await sign_koji_package(nvr_arch)
                koji_session.writeSignedRPM(nvr_arch, settings.sigul_key_id)

        build.signed = True
        await build.save()


@atomic()
async def atomic_check_build_status(build: Build):
    if build.koji_id:
        task_info = koji_session.getTaskInfo(build.koji_id, request=True)
        if task_info["state"] == koji.TASK_STATES["CLOSED"]:
            build.status = BuildStatus.SUCCEEDED
            await build.save()

            package = await Package.filter(id=build.package_id).get()
            package.last_build = datetime.datetime.now()
            await package.save()
        elif task_info["state"] == koji.TASK_STATES["CANCELED"]:
            build.status = BuildStatus.CANCELLED
            await build.save()
        elif task_info["state"] == koji.TASK_STATES["FAILED"]:
            try:
                task_result = koji_session.getTaskResult(build.koji_id)
                logger.debug(task_result)
            except (koji.BuildError, xmlrpc.client.Fault):
                build.status = BuildStatus.FAILED
            except koji.GenericError:
                build.status = BuildStatus.CANCELLED
            finally:
                await build.save()
    elif build.mbs_id:
        build_info = await mbs_client.get_build(build.mbs_id)
        state = build_info["state_name"]
        if state == "ready":
            build.status = BuildStatus.SUCCEEDED
            await build.save()

            package = await Package.filter(id=build.package_id).get()
            package.last_build = datetime.datetime.now()
            await package.save()
        elif state == "failed":
            build.status = BuildStatus.FAILED
            await build.save()


async def check_build_status():
    while True:
        logger.debug("[*] Running periodic task: check_build_status")

        try:
            builds = await Build.filter(status=BuildStatus.BUILDING).all()
            for build in builds:
                await atomic_check_build_status(build)
        except Exception as e:
            logger.error(e)

        # run every 5 minutes
        await asyncio.sleep(60 * 5)


async def sign_unsigned_builds():
    if not settings.disable_sigul:
        while True:
            logger.debug("[*] Running periodic task: sign_unsigned_builds")

            try:
                builds = await Build.filter(signed=False, status=BuildStatus.SUCCEEDED).prefetch_related("package").all()
                for build in builds:
                    await atomic_sign_unsigned_builds(build)
            except Exception as e:
                logger.error(e)

        # run every 5 minutes
            await asyncio.sleep(60 * 5)
