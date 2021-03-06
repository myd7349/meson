# Copyright 2018 The Meson development team

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import re
import os, os.path, pathlib
import shutil

from . import ExtensionModule, ModuleReturnValue

from .. import build, dependencies, mesonlib, mlog
from ..interpreterbase import permittedKwargs
from ..interpreter import ConfigurationDataHolder


COMPATIBILITIES = ['AnyNewerVersion', 'SameMajorVersion', 'SameMinorVersion', 'ExactVersion']

# Taken from https://github.com/Kitware/CMake/blob/master/Modules/CMakePackageConfigHelpers.cmake
PACKAGE_INIT_BASE = '''
####### Expanded from \\@PACKAGE_INIT\\@ by configure_package_config_file() #######
####### Any changes to this file will be overwritten by the next CMake run ####
####### The input file was @inputFileName@ ########
get_filename_component(PACKAGE_PREFIX_DIR "${CMAKE_CURRENT_LIST_DIR}/@PACKAGE_RELATIVE_PATH@" ABSOLUTE)
'''
PACKAGE_INIT_EXT = '''
# Use original install prefix when loaded through a "/usr move"
# cross-prefix symbolic link such as /lib -> /usr/lib.
get_filename_component(_realCurr "${CMAKE_CURRENT_LIST_DIR}" REALPATH)
get_filename_component(_realOrig "@absInstallDir@" REALPATH)
if(_realCurr STREQUAL _realOrig)
  set(PACKAGE_PREFIX_DIR "@installPrefix@")
endif()
unset(_realOrig)
unset(_realCurr)
'''


class CmakeModule(ExtensionModule):
    cmake_detected = False
    cmake_root = None

    def __init__(self, interpreter):
        super().__init__(interpreter)
        self.snippets.add('configure_package_config_file')

    def detect_voidp_size(self, compilers, env):
        compiler = compilers.get('c', None)
        if not compiler:
            compiler = compilers.get('cpp', None)

        if not compiler:
            raise mesonlib.MesonException('Requires a C or C++ compiler to compute sizeof(void *).')

        return compiler.sizeof('void *', '', env)

    def detect_cmake(self):
        if self.cmake_detected:
            return True

        cmakebin = dependencies.ExternalProgram('cmake', silent=False)
        p, stdout, stderr = mesonlib.Popen_safe(cmakebin.get_command() + ['--system-information', '-G', 'Ninja'])[0:3]
        if p.returncode != 0:
            mlog.log('error retrieving cmake informations: returnCode={0} stdout={1} stderr={2}'.format(p.returncode, stdout, stderr))
            return False

        match = re.search('\nCMAKE_ROOT \\"([^"]+)"\n', stdout.strip())
        if not match:
            mlog.log('unable to determine cmake root')
            return False

        cmakePath = pathlib.PurePath(match.group(1))
        self.cmake_root = os.path.join(*cmakePath.parts)
        self.cmake_detected = True
        return True

    @permittedKwargs({'version', 'name', 'compatibility', 'install_dir'})
    def write_basic_package_version_file(self, state, _args, kwargs):
        version = kwargs.get('version', None)
        if not isinstance(version, str):
            raise mesonlib.MesonException('Version must be specified.')

        name = kwargs.get('name', None)
        if not isinstance(name, str):
            raise mesonlib.MesonException('Name not specified.')

        compatibility = kwargs.get('compatibility', 'AnyNewerVersion')
        if not isinstance(compatibility, str):
            raise mesonlib.MesonException('compatibility is not string.')
        if compatibility not in COMPATIBILITIES:
            raise mesonlib.MesonException('compatibility must be either AnyNewerVersion, SameMajorVersion or ExactVersion.')

        if not self.detect_cmake():
            raise mesonlib.MesonException('Unable to find cmake')

        pkgroot = kwargs.get('install_dir', None)
        if pkgroot is None:
            pkgroot = os.path.join(state.environment.coredata.get_builtin_option('libdir'), 'cmake', name)
        if not isinstance(pkgroot, str):
            raise mesonlib.MesonException('Install_dir must be a string.')

        template_file = os.path.join(self.cmake_root, 'Modules', 'BasicConfigVersion-{}.cmake.in'.format(compatibility))
        if not os.path.exists(template_file):
            raise mesonlib.MesonException('your cmake installation doesn\'t support the {} compatibility'.format(compatibility))

        version_file = os.path.join(state.environment.scratch_dir, '{}ConfigVersion.cmake'.format(name))

        conf = {
            'CVF_VERSION': (version, ''),
            'CMAKE_SIZEOF_VOID_P': (str(self.detect_voidp_size(state.compilers, state.environment)), '')
        }
        mesonlib.do_conf_file(template_file, version_file, conf, 'meson')

        res = build.Data(mesonlib.File(True, state.environment.get_scratch_dir(), version_file), pkgroot)
        return ModuleReturnValue(res, [res])

    def create_package_file(self, infile, outfile, PACKAGE_RELATIVE_PATH, extra, confdata):
        package_init = PACKAGE_INIT_BASE.replace('@PACKAGE_RELATIVE_PATH@', PACKAGE_RELATIVE_PATH)
        package_init = package_init.replace('@inputFileName@', infile)
        package_init += extra

        try:
            with open(infile, "r") as fin:
                data = fin.readlines()
        except Exception as e:
            raise mesonlib.MesonException('Could not read input file %s: %s' % (infile, str(e)))

        result = []
        regex = re.compile(r'(?:\\\\)+(?=\\?@)|\\@|@([-a-zA-Z0-9_]+)@')
        for line in data:
            line = line.replace('@PACKAGE_INIT@', package_init)
            line, _missing = mesonlib.do_replacement(regex, line, 'meson', confdata)

            result.append(line)

        outfile_tmp = outfile + "~"
        with open(outfile_tmp, "w", encoding='utf-8') as fout:
            fout.writelines(result)

        shutil.copymode(infile, outfile_tmp)
        mesonlib.replace_if_different(outfile, outfile_tmp)

    @permittedKwargs({'input', 'name', 'install_dir', 'configuration'})
    def configure_package_config_file(self, interpreter, state, args, kwargs):
        if args:
            raise mesonlib.MesonException('configure_package_config_file takes only keyword arguments.')

        if 'input' not in kwargs:
            raise mesonlib.MesonException('configure_package_config_file requires "input" keyword.')
        inputfile = kwargs['input']
        if isinstance(inputfile, list):
            if len(inputfile) != 1:
                m = "Keyword argument 'input' requires exactly one file"
                raise mesonlib.MesonException(m)
            inputfile = inputfile[0]
        if not isinstance(inputfile, (str, mesonlib.File)):
            raise mesonlib.MesonException("input must be a string or a file")
        if isinstance(inputfile, str):
            inputfile = mesonlib.File.from_source_file(state.environment.source_dir, state.subdir, inputfile)

        ifile_abs = inputfile.absolute_path(state.environment.source_dir, state.environment.build_dir)

        if 'name' not in kwargs:
            raise mesonlib.MesonException('"name" not specified.')
        name = kwargs['name']

        (ofile_path, ofile_fname) = os.path.split(os.path.join(state.subdir, '{}Config.cmake'.format(name)))
        ofile_abs = os.path.join(state.environment.build_dir, ofile_path, ofile_fname)

        if 'install_dir' not in kwargs:
            install_dir = os.path.join(state.environment.coredata.get_builtin_option('libdir'), 'cmake', name)
        if not isinstance(install_dir, str):
            raise mesonlib.MesonException('"install_dir" must be a string.')

        if 'configuration' not in kwargs:
            raise mesonlib.MesonException('"configuration" not specified.')
        conf = kwargs['configuration']
        if not isinstance(conf, ConfigurationDataHolder):
            raise mesonlib.MesonException('Argument "configuration" is not of type configuration_data')

        prefix = state.environment.coredata.get_builtin_option('prefix')
        abs_install_dir = install_dir
        if not os.path.isabs(abs_install_dir):
            abs_install_dir = os.path.join(prefix, install_dir)

        PACKAGE_RELATIVE_PATH = os.path.relpath(prefix, abs_install_dir)
        extra = ''
        if re.match('^(/usr)?/lib(64)?/.+', abs_install_dir):
            extra = PACKAGE_INIT_EXT.replace('@absInstallDir@', abs_install_dir)
            extra = extra.replace('@installPrefix@', prefix)

        self.create_package_file(ifile_abs, ofile_abs, PACKAGE_RELATIVE_PATH, extra, conf.held_object)
        conf.mark_used()

        conffile = os.path.normpath(inputfile.relative_name())
        if conffile not in interpreter.build_def_files:
            interpreter.build_def_files.append(conffile)

        res = build.Data(mesonlib.File(True, ofile_path, ofile_fname), install_dir)
        interpreter.build.data.append(res)

        return res

def initialize(*args, **kwargs):
    return CmakeModule(*args, **kwargs)
