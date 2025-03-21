#
# Copyright (c) 2013 Juniper Networks, Inc. All rights reserved.
#

import json
import os
import re
from SCons.Builder import Builder
from SCons.Action import Action
from SCons.Errors import convert_to_BuildError, BuildError
from SCons.Script import AddOption, GetOption, SetOption
from SCons.Node import Alias
from distutils.spawn import find_executable
import SCons.Util
import subprocess
import sys
import datetime
import time
import platform
import getpass
import warnings
import shlex
import multiprocessing


def _ensure_str(s):
    if isinstance(s, bytes):
        return s.decode()
    return s


def GetPyVersion(env):
    return '0.1.dev0'


def GetTestEnvironment(test):
    env = {}
    try:
        with open('controller/ci_unittests.json') as json_file:
            d = json.load(json_file)
            for e in d["contrail-control"]["environment"]:
                for t in e["tests"]:
                    if re.compile(t).match(test):
                        for tup in e["tuples"]:
                            tokens = tup.split("=")
                            env[tokens[0]] = tokens[1]
    except Exception:
        pass
    return env


def RunUnitTest(env, target, source, timeout=300):
    if 'BUILD_ONLY' in env['ENV']:
        return

    if 'CONTRAIL_UT_TEST_TIMEOUT' in env['ENV']:
        timeout = int(env['ENV']['CONTRAIL_UT_TEST_TIMEOUT'])

    test = str(source[0].abspath)
    logfile = open(target[0].abspath, 'w')
    #    env['_venv'] = {target: venv}
    tgt = target[0].name
    if '_venv' in env and tgt in env['_venv'] and env['_venv'][tgt]:
        cmd = ['/bin/bash', '-c', 'source %s/bin/activate && %s' % (
               env[env['_venv'][tgt]]._path, test)]
    else:
        cmd = [test]

    ShEnv = env['ENV'].copy()
    ShEnv.update({env['ENV_SHLIB_PATH']: 'build/lib',
                  'DB_ITERATION_TO_YIELD': '1',
                  'TOP_OBJECT_PATH': env['TOP'][1:]})

    ShEnv.update(GetTestEnvironment(test))
    # Use gprof unless NO_HEAPCHECK is set or in CentOS
    heap_check = 'NO_HEAPCHECK' not in ShEnv
    if heap_check:
        ShEnv['HEAPCHECK'] = 'normal'
        ShEnv['PPROF_PATH'] = 'build/bin/pprof'
        # Fix for frequent crash in gperftools ListerThread during exit
        # https://code.google.com/p/gperftools/issues/detail?id=497
        ShEnv['LD_BIND_NOW'] = '1'

    if 'CONCURRENCY_CHECK_ENABLE' not in ShEnv:
        ShEnv['CONCURRENCY_CHECK_ENABLE'] = 'true'
    proc = subprocess.Popen(cmd, stdout=logfile, stderr=logfile, env=ShEnv)

    # 60 second timeout
    for i in range(timeout):
        code = proc.poll()
        if code is not None:
            break
        time.sleep(1)

    if code is None:
        proc.kill()
        logfile.write('[  TIMEOUT  ] ')
        print(test + '\033[91m' + " TIMEOUT" + '\033[0m')
        raise convert_to_BuildError(code)
        return

    if code == 0:
        print(test + '\033[94m' + " PASS" + '\033[0m')
    else:
        logfile.write('[  FAILED  ] ')
        if code < 0:
            logfile.write('Terminated by signal: ' + str(-code) + '\n')
        else:
            logfile.write('Program returned ' + str(code) + '\n')
        print(test + '\033[91m' + " FAIL" + '\033[0m')
        raise convert_to_BuildError(code)


def TestSuite(env, target, source):
    if not len(source):
        return None

    skip_list = []
    skipfile = GetOption('skip_tests')
    if skipfile and os.path.isfile(skipfile):
        with open(skipfile) as f:
            skip_list = f.readlines()
        skip_list = [test.strip() for test in skip_list]

    for test in env.Flatten(source):
        # UnitTest() may have tagged tests with skip_run attribute
        if getattr(test.attributes, 'skip_run', False) or test.name in skip_list:
            continue

        xml_path = test.abspath + '.xml'
        log_path = test.abspath + '.log'
        env.tests.add_test(node_path=log_path, xml_path=xml_path, log_path=log_path)

        # GTest framework uses environment variables to configure how to write
        # the test output, with GTEST_OUTPUT variable. Make sure targets
        # don't share their environments, so that GTEST_OUTPUT is not
        # overwritten.
        isolated_env = env['ENV'].copy()
        isolated_env['GTEST_OUTPUT'] = 'xml:' + xml_path
        cmd = env.Command(log_path, test, RunUnitTest, ENV=isolated_env)

        # If BUILD_ONLY set, do not alias foo.log target, to avoid
        # invoking the RunUnitTest() as a no-op (i.e., this avoids
        # some log clutter)
        if 'BUILD_ONLY' in env['ENV']:
            env.Alias(target, test)
        else:
            env.AlwaysBuild(cmd)
            env.Alias(target, cmd)
    return target


def GetVncAPIPkg(env):
    return '/api-lib/dist/contrail-api-client-0.1.dev0.tar.gz'


pyver = GetPyVersion(dict())
sdist_default_depends = [
    '/config/common/dist/contrail-config-common-%s.tar.gz' % pyver,
    '/tools/sandesh/library/python/dist/sandesh-%s.tar.gz' % pyver,
    '/sandesh/common/dist/sandesh-common-%s.tar.gz' % pyver,
]


# SetupPyTestSuiteWithDeps
#
# Function to provide consistent 'python setup.py run_tests' interface
#
# The var *args is expected to contain a list of dependencies. If
# *args is empty, then above sdist_default_depends + the vnc_api tgz
# is used.
#
# This method is mostly to be used by SetupPyTestSuite(), but there
# is one special-case instance (controller/src/api-lib) that needs
# to use this builder directly, so that it can provide explicit list
# of dependencies.
#
def SetupPyTestSuiteWithDeps(env, sdist_target, *args, **kwargs):
    use_tox = kwargs['use_tox'] if 'use_tox' in kwargs else False

    buildspace_link = os.environ.get('CONTRAIL_REPO')
    if buildspace_link:
        # in CI environment shebang limit exceeds for python
        # in easy_install/pip, reach to it via symlink
        top_dir = env.Dir(buildspace_link + '/' + env.Dir('.').path)
    else:
        top_dir = env.Dir('.')

    cmd_base = 'bash -c "set -o pipefail && cd ' + env.Dir(top_dir).path + ' && %s 2>&1 | tee %s.log"'

    # if BUILD_ONLY, we create a "pass through" dependency... the test target will end up depending
    # (only) on the original sdist target
    if 'BUILD_ONLY' in env['ENV']:
        test_cmd = sdist_target
    else:
        if use_tox:
            test_cmd = 'tox'
            skipfile = GetOption('skip_tests')
            if skipfile and os.path.isfile(skipfile):
                test_cmd += ' -- --blacklist-file ' + skipfile
        else:
            # NOTE: there is no UT skips in this case. But this case is not used in the code now.
            test_cmd = 'python3 setup.py run_tests'
        test_cmd = env.Command('test.log', sdist_target, cmd_base % (test_cmd, "test"))

    # If *args is not empty, move all arguments to kwargs['sdist_depends']
    # and issue a warning. Also make sure we are not using old and new method
    # of passing dependencies.
    if len(args) and 'sdist_depends' in kwargs:
        print("Do not both pass dependencies as *args"
              "and use sdist_depends at the same time.")
        sys.exit(1)

    # during transition we have to support both types of targets
    # as dependencies. This function allows us to mix both SCons targets
    # and file paths.
    def _rewrite_file_dependencies(deps):
        """Update direct file dependencies to prepend build path"""
        # file dependencies need absulute paths
        file_depends = [env['TOP'] + x for x in deps if x.startswith('/')]
        # explicitly define each target as Alias, in case it hasn't yet been
        # defined in SConscript.
        scons_depends = [env.Alias(x) for x in deps if not x.startswith('/')]
        return file_depends + scons_depends

    if len(args):
        warnings.warn("Don't pass dependencies as arguments pointing"
                      " to tarballs, instead pass scons aliases"
                      " as sdist_depends.")
        full_depends = _rewrite_file_dependencies(env.Flatten(args))
    else:
        full_depends = _rewrite_file_dependencies(kwargs['sdist_depends'])

    # When BUILD_ONLY is defined, test_cmd is replaced with
    # sdist_target - that can lead to circular dependencies when tests
    # depend on other components.
    if 'BUILD_ONLY' not in env['ENV']:
        env.Depends(test_cmd, full_depends)

    d = env.Dir('.').srcnode().path
    env.Alias(d + ':test', test_cmd)
    # env.Depends('test', test_cmd) # XXX This may need to be restored

    xml_path = env.Dir(".").abspath + "/test-results.xml"
    log_path = env.Dir(".").abspath + "/test.log"
    env.tests.add_test(node_path=log_path, xml_path=xml_path, log_path=log_path)


# SetupPyTestSuite()
#
# General entry point for setting up 'python setup.py run_tests'. If
# using this method, the default dependencies are assumed. Any
# additional arguments in *args are *additional* dependencies
#
def SetupPyTestSuite(env, sdist_target, *args, **kwargs):
    sdist_depends = sdist_default_depends + [env.GetVncAPIPkg()]
    if len(args):
        sdist_depends += args

    env.SetupPyTestSuiteWithDeps(sdist_target,
                                 sdist_depends=sdist_depends, **kwargs)


def setup_venv(env, target, venv_name, path=None, is_py3=False):
    p = path
    if not p:
        ws_link = os.environ.get('CONTRAIL_REPO')
        if ws_link:
            p = ws_link + "/build/" + env['OPT']
        else:
            p = env.Dir(env['TOP']).abspath

    tdir = '/tmp/cache/%s/systemless_test' % getpass.getuser()
    shell_cmd = ' && '.join([
        'cd %s' % p,
        'mkdir -p %s' % tdir,
        '[ -f %s/ez_setup-0.9.tar.gz ] || curl -o %s/ez_setup-0.9.tar.gz https://pypi.python.org/packages/source/e/ez_setup/ez_setup-0.9.tar.gz' % (tdir, tdir),
        '[ -d ez_setup-0.9 ] || tar xzf %s/ez_setup-0.9.tar.gz' % tdir,
        '[ -f %s/redis-2.6.13.tar.gz ] || (cd %s && wget https://storage.googleapis.com/google-code-archive-downloads/v2/code.google.com/redis/redis-2.6.13.tar.gz)' % (tdir, tdir),
        '[ -d ../redis-2.6.13 ] || (cd .. && tar xzf %s/redis-2.6.13.tar.gz)' % tdir,
        '[ -f testroot/bin/redis-server ] || ( cd ../redis-2.6.13 && make PREFIX=%s/testroot install)' % p,
        'virtualenv %s',
    ])

    # Create python3 virtualenv
    if is_py3:
        shell_cmd += ' --python=python3'

    for t, v in zip(target, venv_name):
        cmd = env.Command(v, '', shell_cmd % (v,))
        env.Alias(t, cmd)
        cmd._path = '/'.join([p, v])
        env[t] = cmd
    return target


def UnitTest(env, name, sources, **kwargs):
    test_env = env.Clone()

    if 'NO_HEAPCHECK' not in env['ENV']:
        test_env.Append(LIBPATH='#/build/lib')
        test_env.Append(LIBS=['tcmalloc'])
    return test_env.Program(name, sources)


# we are not interested in source files for the dependency, but rather
# to force rebuilds. Pass an empty source to the env.Command, to break
# circular dependencies.
# XXX: This should be rewritten using SCons Value nodes (for generating
# build info itself) and Builder for managing targets.
def GenerateBuildInfoCode(env, target, source, path):
    o = env.Command(target=target, source=[], action=BuildInfoAction)

    # TODO: re-think this
    # if we are running under CI or jenkins-driven CB/OB build,
    # we do NOT want to use AlwaysBuild, as it triggers unnecessary
    # rebuilds.
    # if IsAutomatedBuild:
    env.AlwaysBuild(o)

    return


# If contrail-controller (i.e., #controller/) is present, determine
# git hash of head and get base version from version.info, else use
# hard-coded values.
def GetBuildVersion(env):
    # Fetch git version
    controller_path = env.Dir('#controller').path
    if os.path.exists(controller_path):
        p = subprocess.Popen('cd %s && git rev-parse --short HEAD' % controller_path,
                             stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE,
                             shell='True')
        git_hash, _ = p.communicate()
        git_hash = _ensure_str(git_hash).strip()
    else:
        # Or should we look for vrouter, tools/build, or ??
        git_hash = 'noctrlr'

    # Fetch build version
    file_path = env.File('#/controller/src/base/version.info').abspath
    if os.path.exists(file_path):
        f = open(file_path)
        base_ver = (f.readline()).strip()
    else:
        base_ver = "3.0"

    return git_hash, base_ver


def GetBuildInfoData(env, target, source):
    try:
        build_user = os.environ['USER']
    except KeyError:
        build_user = "unknown"

    try:
        build_host = env['HOSTNAME']
    except KeyError:
        build_host = "unknown"

    # Fetch Time in UTC
    build_time = str(datetime.datetime.utcnow())

    build_git_info, build_version = GetBuildVersion(env)

    # build json string containing build information
    info = {
        'build-version': build_version,
        'build-time': build_time,
        'build-user': build_user,
        'build-hostname': build_host
    }

    return json.dumps({'build-info': [info]})


def BuildInfoAction(env, target, source):
    build_dir = target[0].dir.path
    jsdata = GetBuildInfoData(env, target, source)

    h_code = """
/*
 * Autogenerated file. DO NOT EDIT
 */
#ifndef ctrlplane_buildinfo_h
#define ctrlplane_buildinfo_h
#include <string>
extern const std::string BuildInfo;
#endif // ctrlplane_buildinfo_h"

"""

    cc_code = """
/*
 * Autogenerated file. DO NOT EDIT.
 */
#include "buildinfo.h"

const std::string BuildInfo = "%(json)s";
""" % {'json': jsdata.replace('"', "\\\"")}

    with open(os.path.join(build_dir, 'buildinfo.h'), 'w') as h_file:
        h_file.write(h_code)

    with open(os.path.join(build_dir, 'buildinfo.cc'), 'w') as cc_file:
        cc_file.write(cc_code)


def GenerateBuildInfoCCode(env, target, source, path):
    build_dir = path
    jsdata = GetBuildInfoData(env, target, source)

    c_code = """
/*
 * Autogenerated file. DO NOT EDIT.
 */

const char *ContrailBuildInfo = "%(json)s";
""" % {'json': jsdata.replace('"', "\\\"")}

    with open(os.path.join(build_dir, target[0]), 'w') as c_file:
        c_file.write(c_code)


def GenerateBuildInfoPyCode(env, target, source, path):
    try:
        build_user = getpass.getuser()
    except KeyError:
        build_user = "unknown"

    try:
        build_host = env['HOSTNAME']
    except KeyError:
        build_host = "unknown"

    # Fetch Time in UTC
    build_time = datetime.datetime.utcnow()

    build_git_info, build_version = GetBuildVersion(env)

    # build json string containing build information
    build_info = "{\\\"build-info\\\" : [{\\\"build-version\\\" : \\\"" + str(build_version) + "\\\", \\\"build-time\\\" : \\\"" + str(build_time) + "\\\", \\\"build-user\\\" : \\\"" + build_user + "\\\", \\\"build-hostname\\\" : \\\"" + build_host + "\\\", "
    py_code = "build_info = \"" + build_info + "\"\n"
    with open(path + '/buildinfo.py', 'w') as py_file:
        py_file.write(py_code)

    return target


def Basename(path):
    return path.rsplit('.', 1)[0]


# ExtractCpp Method
def ExtractCppFunc(env, filelist):
    CppSrcs = []
    for target in filelist:
        fname = str(target)
        ext = fname.rsplit('.', 1)[1]
        if ext == 'cpp' or ext == 'cc':
            CppSrcs.append(fname)
    return CppSrcs


def ExtractCFunc(env, filelist):
    CSrcs = []
    for target in filelist:
        fname = str(target)
        ext = fname.rsplit('.', 1)[1]
        if ext == 'c':
            CSrcs.append(fname)
    return CSrcs


def ExtractHeaderFunc(env, filelist):
    Headers = []
    for target in filelist:
        fname = str(target)
        ext = fname.rsplit('.', 1)[1]
        if ext == 'h':
            Headers.append(fname)
    return Headers


def ProtocDescBuilder(target, source, env):
    if not env.Detect('protoc'):
        raise SCons.Errors.StopError(
            'protoc Compiler not detected on system')
    etcd_incl = os.environ.get('CONTRAIL_ETCD_INCL')
    if etcd_incl:
        protoc = env.Dir('#/third_party/grpc/bins/opt/protobuf').abspath + '/protoc'
        protop = ' --proto_path=build/include/ '
    else:
        protoc = env.WhereIs('protoc')
        protop = ' --proto_path=/usr/include/ '
    protoc_cmd = protoc + ' --descriptor_set_out=' + \
        str(target[0]) + ' --include_imports ' + \
        ' --proto_path=controller/src/' + \
        protop + \
        ' --proto_path=src/contrail-analytics/contrail-collector/ ' + \
        str(source[0])
    print(protoc_cmd)
    code = subprocess.call(protoc_cmd, shell=True)
    if code != 0:
        raise SCons.Errors.StopError(
            'protobuf desc generation failed')


def ProtocSconsEnvDescFunc(env):
    descbuild = Builder(action=ProtocDescBuilder)
    env.Append(BUILDERS={'ProtocDesc': descbuild})


def ProtocGenDescFunc(env, file):
    ProtocSconsEnvDescFunc(env)
    suffixes = ['.desc']
    basename = Basename(file)
    targets = map(lambda suffix: basename + suffix, suffixes)
    return env.ProtocDesc(targets, file)


# ProtocCpp Methods
def ProtocCppBuilder(target, source, env):
    spath = str(source[0]).rsplit('/', 1)[0] + "/"
    if not env.Detect('protoc'):
        raise SCons.Errors.StopError(
            'protoc Compiler not detected on system')
    etcd_incl = os.environ.get('CONTRAIL_ETCD_INCL')
    if etcd_incl:
        protoc = env.Dir('#/third_party/grpc/bins/opt/protobuf').abspath + '/protoc'
        protop = ' --proto_path=build/include/ '
    else:
        protoc = env.WhereIs('protoc')
        protop = ' --proto_path=/usr/include/ '
    protoc_cmd = protoc + protop + \
        ' --proto_path=src/contrail-analytics/contrail-collector/ ' + \
        '--proto_path=controller/src/ --proto_path=' + \
        spath + ' --cpp_out=' + str(env.Dir(env['TOP'])) + \
        env['PROTOC_MAP_TGT_DIR'] + ' ' + \
        str(source[0])
    print(protoc_cmd)
    code = subprocess.call(protoc_cmd, shell=True)
    if code != 0:
        raise SCons.Errors.StopError(
            'protobuf code generation failed')


def ProtocSconsEnvCppFunc(env):
    cppbuild = Builder(action=ProtocCppBuilder)
    env.Append(BUILDERS={'ProtocCpp': cppbuild})


def ProtocGenCppMapTgtDirFunc(env, file, target_root=''):
    if target_root == '':
        env['PROTOC_MAP_TGT_DIR'] = ''
    else:
        env['PROTOC_MAP_TGT_DIR'] = '/' + target_root
    ProtocSconsEnvCppFunc(env)
    suffixes = ['.pb.h', '.pb.cc']
    basename = Basename(file)
    targets = map(lambda suffix: basename + suffix, suffixes)
    return env.ProtocCpp(targets, file)


def ProtocGenCppFunc(env, file):
    return (ProtocGenCppMapTgtDirFunc(env, file, ''))


# When doing parallel build, scons will sometimes try to invoke the
# sandesh compiler while sandesh itself is still being compiled and
# linked. This results in a 'text file busy' error, and the build
# aborts.
# To avoid this, a 'wait for it' loop... we run 'sandesh -version',
# and sleep for one sec before retry if it fails.
#
# This is a terrible hack, and should be fixed, but all attempts to
# get scons to recognize the dependency on the sandesh compailer have
# so far been fruitless.
#
def wait_for_sandesh_install(env):
    rc = 0
    while (rc != 1):
        with open(os.devnull, "w") as f:
            try:
                rc = subprocess.call([env['SANDESH'], '-version'], stdout=f, stderr=f)
            except Exception:
                rc = 0
        if rc != 1:
            print('scons: warning: sandesh -version returned %d, retrying' % rc)
            time.sleep(1)


if hasattr(SCons.Warnings, "Warning"):
    # scons 3.x
    class SandeshWarning(SCons.Warnings.Warning):
        pass
else:
    # scons 4.x
    class SandeshWarning(SCons.Warnings.SConsWarning):
        pass


class SandeshCodeGeneratorError(SandeshWarning):
    pass


# SandeshGenDoc Methods
def SandeshDocBuilder(target, source, env):
    opath = target[0].dir.path
    wait_for_sandesh_install(env)
    code = subprocess.call(
        env['SANDESH'] + ' --gen doc -I controller/src/ -I src/contrail-common -out ' +
        opath + " " + source[0].path, shell=True)
    if code != 0:
        raise SCons.Errors.StopError(SandeshCodeGeneratorError,
                                     'SandeshDoc documentation generation failed')


def SandeshSconsEnvDocFunc(env):
    docbuild = Builder(action=Action(SandeshDocBuilder, 'SandeshDocBuilder $SOURCE -> $TARGETS'))
    env.Append(BUILDERS={'SandeshDoc': docbuild})


def SandeshGenDocFunc(env, filepath, target=''):
    SandeshSconsEnvDocFunc(env)
    suffixes = ['.html',
                '_index.html',
                '_logs.html',
                '_logs.doc.schema.json',
                '_logs.emerg.html',
                '_logs.emerg.doc.schema.json',
                '_logs.alert.html',
                '_logs.alert.doc.schema.json',
                '_logs.crit.html',
                '_logs.crit.doc.schema.json',
                '_logs.error.html',
                '_logs.error.doc.schema.json',
                '_logs.warn.html',
                '_logs.warn.doc.schema.json',
                '_logs.notice.html',
                '_logs.notice.doc.schema.json',
                '_logs.info.html',
                '_logs.info.doc.schema.json',
                '_logs.debug.html',
                '_logs.debug.doc.schema.json',
                '_logs.invalid.html',
                '_logs.invalid.doc.schema.json',
                '_uves.html',
                '_uves.doc.schema.json',
                '_traces.html',
                '_traces.doc.schema.json',
                '_introspect.html',
                '_introspect.doc.schema.json',
                '_stats_tables.json']
    basename = Basename(filepath)
    path_split = basename.rsplit('/', 1)
    if len(path_split) == 2:
        filename = path_split[1]
    else:
        filename = path_split[0]
    targets = [target + 'gen-doc/' + filename + suffix for suffix in suffixes]
    env.Depends(targets, '#build/bin/sandesh' + env['PROGSUFFIX'])
    return env.SandeshDoc(targets, filepath)


# SandeshGenOnlyCpp Methods
def SandeshOnlyCppBuilder(target, source, env):
    # file name w/o .sandesh
    sname = os.path.splitext(source[0].name)[0]
    html_cpp_name = os.path.join(target[0].dir.path, sname + '_html.cpp')

    wait_for_sandesh_install(env)
    code = subprocess.call(env['SANDESH'] + ' --gen cpp -I controller/src/ -I src/contrail-common -out ' +
                           target[0].dir.path + " " + source[0].path, shell=True)
    if code != 0:
        raise SCons.Errors.StopError(SandeshCodeGeneratorError,
                                     'SandeshOnlyCpp code generation failed')
    with open(html_cpp_name, 'a') as html_cpp_file:
        html_cpp_file.write('int ' + sname + '_marker = 0;\n')


def SandeshSconsEnvOnlyCppFunc(env):
    onlycppbuild = Builder(action=Action(SandeshOnlyCppBuilder, 'SandeshOnlyCppBuilder $SOURCE -> $TARGETS'))
    env.Append(BUILDERS={'SandeshOnlyCpp': onlycppbuild})


def SandeshGenOnlyCppFunc(env, file, extra_suffixes=[]):
    SandeshSconsEnvOnlyCppFunc(env)
    suffixes = [
        '_types.h',
        '_types.cpp',
        '_constants.h',
        '_constants.cpp',
        '_html.cpp']

    if extra_suffixes:
        if isinstance(extra_suffixes, str):
            extra_suffixes = [extra_suffixes]
        suffixes += extra_suffixes

    basename = Basename(file)
    targets = [basename + suffix for suffix in suffixes]
    env.Depends(targets, '#build/bin/sandesh' + env['PROGSUFFIX'])
    return env.SandeshOnlyCpp(targets, file)


# SandeshGenCpp Methods
def SandeshCppBuilder(target, source, env):
    opath = target[0].dir.path
    sname = os.path.join(opath, os.path.splitext(source[0].name)[0])

    wait_for_sandesh_install(env)
    code = subprocess.call(
        env['SANDESH'] + ' --gen cpp --gen html -I controller/src/ -I src/contrail-common -out ' +
        opath + " " + source[0].path, shell=True)
    if code != 0:
        raise SCons.Errors.StopError(SandeshCodeGeneratorError,
                                     'SandeshCpp code generation failed')
    tname = sname + "_html_template.cpp"
    hname = os.path.basename(sname + ".xml")
    cname = sname + "_html.cpp"
    if not env.Detect('xxd'):
        raise SCons.Errors.StopError(SandeshCodeGeneratorError,
                                     'xxd not detected on system')
    with open(cname, 'w') as cfile:
        cfile.write('namespace {\n')

    # If there's a need to get rid of shell redirection, one should
    # get rid of calling xxd at all - this feature should be done in native Python code.
    code = subprocess.call('xxd -i ' + hname + ' >> ' + os.path.basename(cname), shell=True, cwd=opath)
    if code != 0:
        raise SCons.Errors.StopError(SandeshCodeGeneratorError,
                                     'xxd generation failed')
    with open(cname, 'a') as cfile:
        cfile.write('}\n')
        with open(tname, 'r') as tfile:
            for line in tfile:
                cfile.write(line)


def SandeshSconsEnvCppFunc(env):
    cppbuild = Builder(action=Action(SandeshCppBuilder, 'SandeshCppBuilder $SOURCE -> $TARGETS'))
    env.Append(BUILDERS={'SandeshCpp': cppbuild})


def SandeshGenCppFunc(env, file, extra_suffixes=[]):
    SandeshSconsEnvCppFunc(env)
    suffixes = [
        '_types.h',
        '_types.cpp',
        '_constants.h',
        '_constants.cpp',
        '_html.cpp']

    if extra_suffixes:
        if isinstance(extra_suffixes, str):
            extra_suffixes = [extra_suffixes]
        suffixes += extra_suffixes

    basename = Basename(file)
    targets = [basename + suffix for suffix in suffixes]
    env.Depends(targets, '#build/bin/sandesh' + env['PROGSUFFIX'])
    return env.SandeshCpp(targets, file)


# SandeshGenC Methods
def SandeshCBuilder(target, source, env):
    # We need to trim the /gen-c/ out of the target path
    opath = os.path.dirname(target[0].dir.path)
    wait_for_sandesh_install(env)
    code = subprocess.call(env['SANDESH'] + ' --gen c -o ' + opath +
                           ' ' + source[0].path, shell=True)
    if code != 0:
        raise SCons.Errors.StopError(SandeshCodeGeneratorError,
                                     'SandeshC code generation failed')


def SandeshSconsEnvCFunc(env):
    cbuild = Builder(action=Action(SandeshCBuilder, 'SandeshCBuilder $SOURCE -> $TARGETS'))
    env.Append(BUILDERS={'SandeshC': cbuild})


def SandeshGenCFunc(env, file):
    SandeshSconsEnvCFunc(env)
    suffixes = ['_types.h', '_types.c']
    basename = Basename(file)
    targets = ['gen-c/' + basename + suffix for suffix in suffixes]
    env.Depends(targets, '#build/bin/sandesh' + env['PROGSUFFIX'])
    return env.SandeshC(targets, file)


# SandeshGenPy Methods
def SandeshPyBuilder(target, source, env):
    opath = target[0].dir.path
    py_opath = os.path.dirname(opath)
    wait_for_sandesh_install(env)
    code = subprocess.call(env['SANDESH'] + ' --gen py:new_style -I controller/src/ -I src/contrail-common -out ' +
                           py_opath + " " + source[0].path, shell=True)
    if code != 0:
        raise SCons.Errors.StopError(SandeshCodeGeneratorError,
                                     'SandeshPy py code generation failed')
    code = subprocess.call(env['SANDESH'] + ' --gen html -I controller/src/ -I src/contrail-common -out ' +
                           opath + " " + source[0].path, shell=True)
    if code != 0:
        raise SCons.Errors.StopError(SandeshCodeGeneratorError,
                                     'SandeshPy html generation failed')


def SandeshSconsEnvPyFunc(env):
    pybuild = Builder(action=Action(SandeshPyBuilder, 'SandeshPyBuilder $SOURCE -> $TARGETS'))
    env.Append(BUILDERS={'SandeshPy': pybuild})


def SandeshGenPyFunc(env, path, target='', gen_py=True):
    SandeshSconsEnvPyFunc(env)
    modules = [
        '__init__.py',
        'constants.py',
        'ttypes.py',
        'http_request.py']
    basename = Basename(path)
    path_split = basename.rsplit('/', 1)
    if len(path_split) == 2:
        mod_dir = path_split[1] + '/'
    else:
        mod_dir = path_split[0] + '/'
    if gen_py:
        targets = [target + 'gen_py/' + mod_dir + module for module in modules]
    else:
        targets = [target + mod_dir + module for module in modules]

    env.Depends(targets, '#build/bin/sandesh' + env['PROGSUFFIX'])
    return env.SandeshPy(targets, path)


# Golang Methods for CNI
def GoBuildFunc(env, mod_path, target):
    # get dependencies
    goenv = os.environ.copy()
    goenv['GOROOT'] = "/usr/local/go"
    goenv['GOBIN'] = env.Dir(env['TOP'] + '/container/cni/bin').abspath

    cmd = 'cd ' + mod_path + '; '
    cmd += goenv['GOROOT'] + '/bin/go install -ldflags "-s -w" ' + target
    code = subprocess.call(cmd, shell=True, env=goenv)
    if code != 0:
        raise SCons.Errors.StopError(SandeshCodeGeneratorError,
                                    'go install failed')


def GoUnitTest(env, mod_path):
    # get dependencies
    goenv = os.environ.copy()
    goenv['GOROOT'] = "/usr/local/go"
    goenv['GOBIN'] = env.Dir(env['TOP'] + '/container/cni/bin').abspath

    cmd = 'cd ' + mod_path + '; '
    cmd += goenv['GOROOT'] + '/bin/go test -count 1 -gcflags=-l ./...'
    code = subprocess.call(cmd, shell=True, env=goenv)
    if code != 0:
        raise SCons.Errors.StopError(SandeshCodeGeneratorError,
                                    'go test failed')


def IFMapBuilderCmd(source, target, env, for_signature):
    output = Basename(source[0].abspath)
    return '%s -f -g ifmap-backend -o %s %s' % (env.File('#src/contrail-api-client/generateds/generateDS.py').abspath, output, source[0])


def IFMapTargetGen(target, source, env):
    suffixes = ['_types.h', '_types.cc', '_parser.cc',
                '_server.cc', '_agent.cc']
    basename = Basename(source[0].abspath)
    targets = [basename + x for x in suffixes]
    return targets, source


def CreateIFMapBuilder(env):
    builder = Builder(generator=IFMapBuilderCmd,
                      src_suffix='.xsd',
                      emitter=IFMapTargetGen)
    env.Append(BUILDERS={'IFMapAutogen': builder})


def DeviceAPIBuilderCmd(source, target, env, for_signature):
    output = Basename(source[0].abspath)
    return './src/contrail-api-client/generateds/generateDS.py -f -g device-api -o %s %s' % (output, source[0])


def DeviceAPITargetGen(target, source, env):
    suffixes = []
    basename = Basename(source[0].abspath)
    targets = map(lambda x: basename + x, suffixes)
    return targets, source


def CreateDeviceAPIBuilder(env):
    builder = Builder(generator=DeviceAPIBuilderCmd,
                      src_suffix='.xsd')
    env.Append(BUILDERS={'DeviceAPIAutogen': builder})


def TypeBuilderCmd(source, target, env, for_signature):
    output = Basename(source[0].abspath)
    return '%s -f -g type -o %s %s' % (env.File('#src/contrail-api-client/generateds/generateDS.py').abspath, output, source[0])


def TypeTargetGen(target, source, env):
    suffixes = ['_types.h', '_types.cc', '_parser.cc']
    basename = Basename(source[0].abspath)
    targets = [basename + x for x in suffixes]
    return targets, source


def CreateTypeBuilder(env):
    builder = Builder(generator=TypeBuilderCmd,
                      src_suffix='.xsd',
                      emitter=TypeTargetGen)
    env.Append(BUILDERS={'TypeAutogen': builder})


# Check for unsupported/buggy compilers.
def CheckBuildConfiguration(conf):
    # gcc 4.7.0 generates buggy code when optimization is turned on.
    opt_level = GetOption('opt')
    if ((opt_level == 'production' or opt_level == 'profile') and
            (conf.env['CC'].endswith("gcc") or conf.env['CC'].endswith("g++")) and
            conf.env['CCVERSION'] == "4.7.0"):
        print("Unsupported/Buggy compiler gcc 4.7.0 for building optimized binaries")
        raise convert_to_BuildError(1)
    # Specific versions of MS C++ compiler are not supported for
    # "production" build.
    if opt_level == 'production' and conf.env['CC'] == 'cl':
        if not VerifyClVersion():
            print("Unsupported MS C++ compiler for building " +
                  "optimized binaries")
            raise convert_to_BuildError(1)
    return conf.Finish()


def VerifyClVersion():
    # Microsoft C++ 19.00.24210 is known to produce incorrectly working Agent
    # in "production" build as it is described in bug #1802130.
    # Undesired behaviour has been mitigated by code change in Agent.
    # However, this compiler version (and all older) are considered "unsafe"
    # and luckily there's no reason to use them. MS VC 2015 Update 3 provides
    # newer version of the compiler.
    minimum_cl_version = [19, 0, 24215, 1]

    # Unfortunately there's no better way to check the CL version
    output = subprocess.check_output(['cl.exe'], stderr=subprocess.STDOUT, encoding='ASCII')
    output = _ensure_str(output)
    regex_string = "Microsoft \(R\) C/C\+\+ [\s\w]*Version ([0-9]+)\." +\
                   "([0-9]+)\.([0-9]+)(?:\.([0-9]+))?[\s\w]*" # noqa
    regex_parser = re.compile(regex_string)
    match = regex_parser.match(output)
    our_cl_version = [int(x or 0) for x in (match.groups())]
    return our_cl_version >= minimum_cl_version


def CppEnableExceptions(env):
    cflags = env['CCFLAGS']
    if '-fno-exceptions' in cflags:
        cflags.remove('-fno-exceptions')
        env.Replace(CCFLAGS=cflags)


# Decide whether to use parallel build, and determine value to use/set.
# Controlled by environment var CONTRAIL_BUILD_JOBS:
#    if set to 'no' or 1, then no parallel build
#    if set to an integer, use it blindly
#    if set to any other string (e.g., 'yes'):
#        compute a reasonable value based on number of CPU's and load avg
#
def determine_job_value():
    if 'CONTRAIL_BUILD_JOBS' not in os.environ:
        return 1

    v = os.environ['CONTRAIL_BUILD_JOBS']
    if v == 'no':
        return 1

    try:
        return int(v)
    except Exception:
        pass

    try:
        ncpu = multiprocessing.cpu_count()
        ncore = ncpu / 2
    except Exception:
        ncore = 1

    (one, five, _) = os.getloadavg()
    avg_load = int(one + five / 2)
    avail = (ncore - avg_load) * 3 / 2
    print("scons: available jobs = %d" % avail)
    return avail


class UnitTestsCollector(object):
    """Unit Test collector and processor

    A small class that abstracts collecting unit tests and their metadata.
    It is used to generate a list of tests from the targets passed to scons,
    to be used by the CI test runner to better report failures.
    """

    def __init__(self):
        self.tests = []

    def add_test(self, node_path, xml_path, log_path):
        self.tests += [{
            "node_path": node_path, "xml_path": xml_path,
            "log_path": log_path}]


def EnsureBuildDependency(env, dependency):
    if not find_executable(dependency):
        raise BuildError(errstr='The \'{}\' utility was not found in the PATH.'.format(dependency))


def SetupBuildEnvironment(conf):
    AddOption('--optimization', '--opt', dest='opt',
              action='store', default='debug',
              choices=['debug', 'production', 'profile'],
              help='optimization level: [debug|production|profile]')

    AddOption('--target', dest='target',
              action='store', default='x86_64',
              choices=['i686', 'x86_64', 'armhf'])

    AddOption('--cpu', dest='cpu',
              action='store',
              choices=['native', 'hsw', 'snb', 'ivb'])

    AddOption('--root', dest='install_root', action='store')
    AddOption('--prefix', dest='install_prefix', action='store')
    AddOption('--pytest', dest='pytest', action='store')
    AddOption('--without-dpdk', dest='without-dpdk',
              action='store_true', default=False)
    AddOption('--skip-tests', dest='skip_tests', action='store', default=None)
    AddOption('--describe-tests', dest='describe-tests',
              action='store_true', default=False)
    AddOption('--describe-aliases', dest='describe-aliases',
              action='store_true', default=False)
    AddOption('--c++', '--cpp', '--std', dest='cpp_standard',
              action='store', default='c++11',
              choices=['c++98', 'c++11', 'c++14', 'c++17', 'c++2a'],
              help='C++ standard[c++98, c++11, c++14, c++17, c++2a]')

    env = CheckBuildConfiguration(conf)

    env.AddMethod(GetPyVersion, "GetPyVersion")

    # Let's decide how many jobs (-jNN) we should use.
    nj = GetOption('num_jobs')
    if nj == 1:
        # Should probably check for CLI over-ride of -j1 (i.e., do not
        # assume 1 means -j not specified).
        nj = determine_job_value()
        if nj > 1:
            print("scons: setting jobs (-j) to %d" % nj)
            SetOption('num_jobs', nj)
            env['NUM_JOBS'] = nj

    env['OPT'] = GetOption('opt')
    env['TARGET_MACHINE'] = GetOption('target')
    env['CPU_TYPE'] = GetOption('cpu')
    env['INSTALL_PREFIX'] = GetOption('install_prefix')
    env['INSTALL_BIN'] = ''
    env['INSTALL_SHARE'] = ''
    env['INSTALL_LIB'] = ''
    env['INSTALL_INIT'] = ''
    env['INSTALL_INITD'] = ''
    env['INSTALL_SYSTEMD'] = ''
    env['INSTALL_CONF'] = ''
    env['INSTALL_SNMP_CONF'] = ''
    env['INSTALL_EXAMPLE'] = ''
    env['PYTHON_INSTALL_OPT'] = ''
    env['INSTALL_DOC'] = ''
    env['CPP_STANDARD'] = GetOption('cpp_standard')

    install_root = GetOption('install_root')
    if install_root:
        env['INSTALL_BIN'] = install_root
        env['INSTALL_SHARE'] = install_root
        env['INSTALL_LIB'] = install_root
        env['INSTALL_INIT'] = install_root
        env['INSTALL_INITD'] = install_root
        env['INSTALL_SYSTEMD'] = install_root
        env['INSTALL_CONF'] = install_root
        env['INSTALL_SNMP_CONF'] = install_root
        env['INSTALL_EXAMPLE'] = install_root
        env['INSTALL_DOC'] = install_root
        env['PYTHON_INSTALL_OPT'] = '--root ' + install_root + ' '

    install_prefix = GetOption('install_prefix')
    if install_prefix:
        env['INSTALL_BIN'] += install_prefix
        env['INSTALL_SHARE'] += install_prefix
        env['INSTALL_LIB'] += install_prefix
        env['INSTALL_INIT'] += install_prefix
        env['INSTALL_INITD'] += install_prefix
        env['INSTALL_SYSTEMD'] += install_prefix
        env['PYTHON_INSTALL_OPT'] += '--prefix ' + install_prefix + ' '
    elif install_root:
        env['INSTALL_BIN'] += '/usr'
        env['INSTALL_SHARE'] += '/usr'
        env['INSTALL_LIB'] += '/usr'
        env['PYTHON_INSTALL_OPT'] += '--prefix /usr '
    else:
        env['INSTALL_BIN'] += '/usr/local'

    env['INSTALL_BIN'] += '/bin'
    env['INSTALL_SHARE'] += '/share'
    env['INSTALL_LIB'] += '/lib'
    env['INSTALL_INIT'] += '/etc/init'
    env['INSTALL_INITD'] += '/etc/init.d'
    env['INSTALL_SYSTEMD'] += '/lib/systemd/system'
    env['INSTALL_CONF'] += '/etc/contrail'
    env['INSTALL_SNMP_CONF'] += '/etc/snmp'
    env['INSTALL_EXAMPLE'] += '/usr/share/contrail'
    env['INSTALL_DOC'] += '/usr/share/doc'

    env['ENV_SHLIB_PATH'] = 'LD_LIBRARY_PATH'

    if env.get('TARGET_MACHINE') == 'i686':
        env.Append(CCFLAGS='-march=' + 'i686')
    elif env.get('TARGET_MACHINE') == 'armhf' or platform.machine().startswith('arm'):
        env.Append(CCFLAGS=['-DTBB_USE_GCC_BUILTINS=1', '-D__TBB_64BIT_ATOMICS=0'])

    env['TOP_BIN'] = '#build/bin'
    env['TOP_INCLUDE'] = '#build/include'
    env['TOP_LIB'] = '#build/lib'

    pytest = GetOption('pytest')
    if pytest:
        env['PYTESTARG'] = pytest
    else:
        env['PYTESTARG'] = None
    env.tests = UnitTestsCollector()

    # Store path to sandesh compiler in the env
    env['SANDESH'] = os.path.join(env.Dir(env['TOP_BIN']).path, 'sandesh' + env['PROGSUFFIX'])

    # Store the hostname in env.
    if 'HOSTNAME' not in env:
        env['HOSTNAME'] = platform.node()

    # Store repo projects in the environment
    proc = subprocess.Popen('repo list', stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell='True')
    repo_out, _ = proc.communicate()
    repo_out = _ensure_str(repo_out)
    repo_lines = repo_out.splitlines()
    repo_list = {}
    for line in repo_lines:
        (path, repo) = line.split(" : ")
        repo_list[path] = repo
    env['REPO_PROJECTS'] = repo_list

    if env['CPP_STANDARD']:
        stdoption = '-std=' + env['CPP_STANDARD']
        env.Append(CXXFLAGS=stdoption)
        if env['CPP_STANDARD'] == 'c++11':
            env.Append(CXXFLAGS='-Wno-deprecated')
            env.Append(CXXFLAGS='-DBOOST_NO_CXX11_SCOPED_ENUMS')

    opt_level = env['OPT']
    if opt_level == 'production':
        env.Append(CCFLAGS='-O3')
        env['TOP'] = '#build/production'
    elif opt_level == 'debug':
        env.Append(CCFLAGS=['-O0', '-DDEBUG'])
        env['TOP'] = '#build/debug'
    elif opt_level == 'profile':
        # Enable profiling through gprof
        env.Append(CCFLAGS=['-O3', '-DDEBUG', '-pg'])
        env.Append(LINKFLAGS=['-pg'])
        env['TOP'] = '#build/profile'

    if "CONTRAIL_COMPILE_WITHOUT_SYMBOLS" not in os.environ:
        env.Append(CCFLAGS='-g')
        env.Append(LINKFLAGS='-g')

    env.Append(BUILDERS={'TestSuite': TestSuite})
    env.Append(BUILDERS={'UnitTest': UnitTest})
    env.Append(BUILDERS={'GenerateBuildInfoCode': GenerateBuildInfoCode})
    env.Append(BUILDERS={'GenerateBuildInfoPyCode': GenerateBuildInfoPyCode})
    env.Append(BUILDERS={'GenerateBuildInfoCCode': GenerateBuildInfoCCode})

    env.Append(BUILDERS={'setup_venv': setup_venv})

    # A few methods to enable/support UTs and BUILD_ONLY
    env.AddMethod(GetVncAPIPkg, 'GetVncAPIPkg')
    env.AddMethod(SetupPyTestSuite, 'SetupPyTestSuite')
    env.AddMethod(SetupPyTestSuiteWithDeps, 'SetupPyTestSuiteWithDeps')
    env.AddMethod(EnsureBuildDependency, 'EnsureBuildDependency')

    env.AddMethod(ExtractCppFunc, "ExtractCpp")
    env.AddMethod(ExtractCFunc, "ExtractC")
    env.AddMethod(ExtractHeaderFunc, "ExtractHeader")
    env.AddMethod(GetBuildVersion, "GetBuildVersion")
    env.AddMethod(ProtocGenDescFunc, "ProtocGenDesc")
    env.AddMethod(ProtocGenCppFunc, "ProtocGenCpp")
    env.AddMethod(ProtocGenCppMapTgtDirFunc, "ProtocGenCppMapTgtDir")
    env.AddMethod(SandeshGenOnlyCppFunc, "SandeshGenOnlyCpp")
    env.AddMethod(SandeshGenCppFunc, "SandeshGenCpp")
    env.AddMethod(SandeshGenCFunc, "SandeshGenC")
    env.AddMethod(SandeshGenPyFunc, "SandeshGenPy")
    env.AddMethod(SandeshGenDocFunc, "SandeshGenDoc")
    env.AddMethod(GoBuildFunc, "GoBuild")
    env.AddMethod(GoUnitTest, "GoUnitTest")
    env.AddMethod(SchemaSyncFunc, "SyncSchema")
    CreateIFMapBuilder(env)
    CreateTypeBuilder(env)
    CreateDeviceAPIBuilder(env)

    symlink_builder = Builder(action="cd ${TARGET.dir} && " +
                              "ln -s ${SOURCE.file} ${TARGET.file}")
    env.Append(BUILDERS={'Symlink': symlink_builder})

    env.AddMethod(CppEnableExceptions, "CppEnableExceptions")

    return env


def resolve_alias_dependencies(env, aliases):
    """Given alias string, return all its leaf dependencies.

    SCons aliases can depend on SCons nodes, or other aliases. Recursively
    resolve aliases to actual dependencies.
    """
    nodes = set()
    for alias in aliases:
        assert isinstance(alias, Alias.Alias)
        for node in alias.children():
            if isinstance(node, Alias.Alias):
                nodes |= (resolve_alias_dependencies(env, [node]))
            else:
                nodes.add(node)
    return nodes


def DescribeTests(env, targets):
    """Given a set of targets, print out JSON Lines encoded tests."""
    node_paths = []
    for target in targets:
        scons_aliases = env.arg2nodes(target)
        nodes = resolve_alias_dependencies(env, scons_aliases)
        node_paths += [n.abspath for n in nodes]

    matched_tests = []
    for test in env.tests.tests:
        path = test['node_path']
        if path in node_paths:
            test['matched'] = True
            matched_tests += [test]
            node_paths.remove(path)

    for test in matched_tests:
        print(json.dumps(test))

    for node_path in node_paths:
        dangling_node = {"node_path": node_path, "matched": False}
        print(json.dumps(dangling_node))


def DescribeAliases():
    print('Available Build Aliases:')
    print('------------------------')
    for alias in sorted(Alias.default_ans.keys()):
        print(alias)


def SchemaSyncBuilder(target, source, env):
    target_path = env.Dir(str(target[0]).rsplit('/', 1)[0] + "/").abspath
    # generate yaml schema
    generateds = env.File('#src/contrail-api-client/generateds/generateDS.py').abspath
    schema_gen_cmd = "python3 %s -f -o %s -g contrail-json-schema %s" % (
        generateds, target_path, str(source[0]))
    code = subprocess.call(schema_gen_cmd, shell=True)
    if code != 0:
        raise SCons.Errors.StopError(
            'Failed to generate yaml schema from xml schema')

    # sync yaml schema
    source_path = env.Dir(str(source[0]).rsplit('/', 1)[0] + "/").abspath
    yaml_schema_path = source_path + "/yaml"
    schema_sync_cmd = "cp -r %s/* %s/" % (target_path, yaml_schema_path)
    code = subprocess.call(schema_sync_cmd, shell=True)
    if code != 0:
        raise SCons.Errors.StopError(
            'Failed to sync generated yaml schema to %s' % yaml_schema_path)

    # Ensure the yaml schema diff is commited
    yaml_schema_status_cmd = "git status --porcelain -- ."
    output = subprocess.check_output(
        yaml_schema_status_cmd, shell=True, cwd=yaml_schema_path)
    output = _ensure_str(output)
    if output != "":
        dec_str = "#" * 80
        print("%s\n\nSchema modified!!!\n\n" % dec_str)
        print(output)
        print("\n\n")
        print("Please add yaml schema changes in %s/* to your commit\n\n%s" %
              (yaml_schema_path, dec_str))
        raise SCons.Errors.StopError(
            "XML and YAML schema's are out of sync!")


def SchemaSyncSconsEnvBuildFunc(env):
    schemabuild = Builder(action=SchemaSyncBuilder)
    env.Append(BUILDERS={'SchemaSyncSconsBuild': schemabuild})


def SchemaSyncFunc(env, target, source):
    SchemaSyncSconsEnvBuildFunc(env)
    return env.SchemaSyncSconsBuild(target, source)
