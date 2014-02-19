from __future__ import print_function, division, absolute_import

import re
import os
import sys
import stat
from glob import glob
from subprocess import call, check_call, check_output
from os.path import basename, join, splitext, isdir, isfile, normpath

from conda_build.config import build_prefix, build_python, PY3K
from conda_build import external
from conda_build import environ
from conda_build import utils
from conda.compat import lchmod
from conda.utils import memoized

if sys.platform.startswith('linux'):
    from conda_build import elf
elif sys.platform == 'darwin':
    from conda_build import macho



def is_obj(path):
    assert sys.platform != 'win32'
    return bool((sys.platform.startswith('linux') and elf.is_elf(path)) or
                (sys.platform == 'darwin' and macho.is_macho(path)))



shebang_pat = re.compile(r'^#!.+$', re.M)
def fix_shebang(f, osx_is_app=False):
    path = join(build_prefix, f)
    if is_obj(path):
        return
    with open(path) as fi:
        try:
            data = fi.read()
        except UnicodeDecodeError: # file is binary
            return
    m = shebang_pat.match(data)
    if not (m and 'python' in m.group()):
        return

    py_exec = (build_prefix + '/python.app/Contents/MacOS/python'
               if sys.platform == 'darwin' and osx_is_app else
               build_prefix + '/bin/' + basename(build_python))
    new_data = shebang_pat.sub('#!' + py_exec, data, count=1)
    if new_data == data:
        return
    print("updating shebang:", f)
    with open(path, 'w') as fo:
        fo.write(new_data)
    os.chmod(path, int('755', 8))


def write_pth(egg_path):
    fn = basename(egg_path)
    with open(join(environ.sp_dir,
                   '%s.pth' % (fn.split('-')[0])), 'w') as fo:
        fo.write('./%s\n' % fn)

def remove_easy_install_pth(preserve_egg_dir=False):
    """
    remove the need for easy-install.pth and finally remove easy-install.pth
    itself
    """
    sp_dir = environ.sp_dir
    for egg_path in glob(join(sp_dir, '*-py*.egg')):
        if isdir(egg_path):
            if preserve_egg_dir:
                write_pth(egg_path)
                continue

            print('found egg dir:', egg_path)
            try:
                os.rename(join(egg_path, 'EGG-INFO/PKG-INFO'),
                          egg_path + '-info')
            except OSError:
                pass
            utils.rm_rf(join(egg_path, 'EGG-INFO'))
            for fn in os.listdir(egg_path):
                if fn == '__pycache__':
                    utils.rm_rf(join(egg_path, fn))
                else:
                    os.rename(join(egg_path, fn), join(sp_dir, fn))

        elif isfile(egg_path):
            print('found egg:', egg_path)
            write_pth(egg_path)

    utils.rm_rf(join(sp_dir, 'easy-install.pth'))


def rm_py_along_so():
    "remove .py (.pyc) files alongside .so or .pyd files"
    for root, dirs, files in os.walk(build_prefix):
        for fn in files:
            if fn.endswith(('.so', '.pyd')):
                name, unused_ext = splitext(fn)
                for ext in '.py', '.pyc':
                    if name + ext in files:
                        os.unlink(join(root, name + ext))


def compile_missing_pyc():
    sp_dir = environ.sp_dir

    need_compile = False
    for root, dirs, files in os.walk(sp_dir):
        for fn in files:
            if fn.endswith('.py') and fn + 'c' not in files:
                need_compile = True
    if need_compile:
        print('compiling .pyc files...')
        utils._check_call([build_python, '-Wi', join(environ.stdlib_dir,
                                                     'compileall.py'),
                           '-q', '-x', 'port_v3', sp_dir])


def post_process(preserve_egg_dir=False):
    remove_easy_install_pth(preserve_egg_dir=preserve_egg_dir)
    rm_py_along_so()
    if not PY3K:
        compile_missing_pyc()


def osx_ch_link(path, link):
    assert path.startswith(build_prefix + '/')
    reldir = utils.rel_lib(path[len(build_prefix) + 1:])

    if link.startswith((build_prefix + '/lib', 'lib', '@executable_path/')):
        return '@loader_path/%s/%s' % (reldir, basename(link))

    if link == '/usr/local/lib/libgcc_s.1.dylib':
        return '/usr/lib/libgcc_s.1.dylib'

def mk_relative_osx(path):
    assert sys.platform == 'darwin' and is_obj(path)
    macho.install_name_change(path, osx_ch_link)

    if path.endswith('.dylib'):
        # note that not every MachO binaries is a "dynamically linked shared
        # library" which have an identification name, a .so C extensions
        # extensions is a "bundle".  One can verify this using the "file"
        # command.
        names = macho.otool(path)
        if names:
            args = ['install_name_tool', '-id', basename(names[0]), path]
            print(' '.join(args))
            check_call(args)

    for name in macho.otool(path):
        assert not name.startswith(build_prefix), path

@memoized
def get_chrpath():
    return external.find_executable('chrpath')

def adjust_rpath(path):
    chrpath = get_chrpath()
    output = check_output([chrpath, '-l', path])
    if output[-1] == '\n':
        output = output[:-1]
    (obj, rpath) = output.split(': ')
    prefix = 'RPATH='
    assert obj == path, (obj, path)
    assert rpath.startswith(prefix), (rpath, prefix)
    rpath = rpath[len(prefix):]

    root_path = utils.get_root_path((path, rpath))
    rel_path = path.replace(root_path, '')
    rel_rpath = rpath.replace(root_path, '')
    lib_path = '%s/lib' % build_prefix

    if rpath == lib_path:
        new_rpath = '$ORIGIN/%s' % normpath(rel_path.count('/') * '../')
    else:
        # .so is linking to somewhere outside of $PREFIX/lib; alter RPATH to
        # point to that location first, then $PREFIX/lib second.
        rel_file = path.replace(build_prefix, '')[1:]
        rel_lib_path = utils.rel_lib(rel_file)
        new_rpath = '$ORIGIN/%s/%s:$ORIGIN/%s' % (
            normpath(rel_path.count('/') * '../'),
            rel_rpath,
            rel_lib_path,
        )

    check_call([chrpath, '-r', new_rpath, path])

def mk_relative(f):
    assert sys.platform != 'win32'
    if f.startswith('bin/'):
        fix_shebang(f)

    path = join(build_prefix, f)
    if sys.platform.startswith('linux') and is_obj(path):
        adjust_rpath(path)

    if sys.platform == 'darwin' and is_obj(path):
        mk_relative_osx(path)


def fix_permissions(files):
    for root, dirs, unused_files in os.walk(build_prefix):
        for dn in dirs:
            os.chmod(join(root, dn), int('755', 8))

    for f in files:
        path = join(build_prefix, f)
        st = os.lstat(path)
        lchmod(path, stat.S_IMODE(st.st_mode) | stat.S_IWUSR) # chmod u+w


def post_build(files):
    print('number of files:', len(files))
    fix_permissions(files)
    for f in files:
        if sys.platform != 'win32':
            mk_relative(f)
