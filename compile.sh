#!/bin/bash
set -e; set -o pipefail; set -m
[ "${DEBUG^^}" != 'TRUE' ] || set -x

# do it explicitely
set -x

# TODO: restore contrail-debug packages (see "debug" docker image)
# TODO: restore contrail-tor-agent

SCONS_OPT=${SCONS_OPT:-production}

# folder with built binaries/libs/docs/data/... files from tf-dev-sandbox container
export BUILD_ROOT=${BUILD_ROOT:-'/buildroot'}

my_dir=$(realpath $(dirname "$0"))
src_root=$(dirname $(dirname "$my_dir"))

src_ver=$(cat $src_root/controller/src/base/version.info)

# now we are creating "BUILD_ROOT" - it will be used in later "docker build" as a "multistage"
rm -rf ${BUILD_ROOT}/
mkdir -p ${BUILD_ROOT}/
echo "$src_ver" > ${BUILD_ROOT}/Version

mkdir -p ${BUILD_ROOT}/usr/bin/tools ${BUILD_ROOT}/opt/contrail/ddp
mkdir -p ${BUILD_ROOT}/usr/bin ${BUILD_ROOT}/usr/lib ${BUILD_ROOT}/opt ${BUILD_ROOT}/opt/python/opserver ${BUILD_ROOT}/usr/bin/tools/
mkdir -p ${BUILD_ROOT}/usr/share/lua/ ${BUILD_ROOT}/usr/local/share/wireshark/ ${BUILD_ROOT}/usr/local/lib64/wireshark/plugins/
mkdir -p ${BUILD_ROOT}/usr/src/modules/
mkdir -p ${BUILD_ROOT}/usr/src/contrail/contrail-web-controller/ ${BUILD_ROOT}/usr/src/contrail/contrail-web-core/

pushd $src_root/
build_jobs=$(nproc --ignore=1)
build_number=$(date +%Y%m%d%H%M)
# compile and pack most components
scons -j "$build_jobs" --opt=$SCONS_OPT --root=${BUILD_ROOT} --without-dpdk --build-number="$build_number" install
# dpdk stuff
scons \
    --opt=$SCONS_OPT \
    -j "$build_jobs" \
    --build-number="$build_number" \
    --dpdk-jobs="$build_jobs" \
    --root=$src_root/BUILD \
    --add-opts=enableMellanox \
    --add-opts=enableN3K \
    vrouter/dpdk
popd

pushd $src_root/contrail-web-core
make package REPO=../contrail-web-core
make package REPO=../contrail-web-controller,webController
popd

# contrail-docs
# Move schema specific files to opserver
for mod_dir in ${BUILD_ROOT}/usr/share/doc/contrail-docs/html/messages/* ; do
    if [[ ! -d $mod_dir ]]; then
        continue
    fi
    mkdir -p ${BUILD_ROOT}/opt/python/opserver/stats_schema/$(basename $mod_dir)
    for statsfile in ${BUILD_ROOT}/usr/share/doc/contrail-docs/html/messages/$(basename $mod_dir)/*_stats_tables.json ; do
        mv $statsfile ${BUILD_ROOT}/opt/python/opserver/stats_schema/$(basename $mod_dir)/
    done
done
# Index files
python3 $src_root/tools/build/generate_doc_index.py ${BUILD_ROOT}/usr/share/doc/contrail-docs/html/messages

# pack vrouter sources
pushd ${BUILD_ROOT}
cd usr/src/vrouter
echo "$src_ver" > version
tar -czf ${BUILD_ROOT}/usr/src/modules/contrail-vrouter.tar.gz .
popd
rm -rf ${BUILD_ROOT}/usr/src/vrouter

####################################################### dpdk stuff
cp $src_root/build/$SCONS_OPT/vrouter/dpdk/contrail-vrouter-dpdk ${BUILD_ROOT}/usr/bin/contrail-vrouter-dpdk
cp $src_root/build/$SCONS_OPT/vrouter/dpdk/dpdk-devbind.py ${BUILD_ROOT}/usr/bin/dpdk_nic_bind.py
cp $src_root/vrouter/dpdk/ddp/mplsogreudp.pkg ${BUILD_ROOT}/opt/contrail/ddp/mplsogreudp.pkg
# tools
cp $src_root//build/$SCONS_OPT/vrouter/dpdk/x86_64-native-linuxapp-gcc/app/testpmd ${BUILD_ROOT}/usr/bin/tools/testpmd

N3KFLOW_DUMP_BINARY="$src_root/build/production/vrouter/dpdk/x86_64-native-linuxapp-gcc/build/app/n3kflow-dump/n3kflow-dump"
cp "$N3KFLOW_DUMP_BINARY" ${BUILD_ROOT}/usr/bin/n3kflow-dump
cp $src_root/build/$SCONS_OPT/vrouter/dpdk/x86_64-native-linuxapp-gcc/app/n3k-info ${BUILD_ROOT}/usr/bin/n3k-info
####################################################### dpdk stuff

# contrail-manifest package
cp $src_root/.repo/manifest.xml ${BUILD_ROOT}/manifest.xml

# for 'unknown' reason most libs are out of BUILD_ROOT
cp -a /root/work/build/lib/lib*.so* ${BUILD_ROOT}/usr/lib/

# webui
cp -rp $src_root/contrail-web-controller/* ${BUILD_ROOT}/usr/src/contrail/contrail-web-controller/
cp -rp $src_root/contrail-web-core/* ${BUILD_ROOT}/usr/src/contrail/contrail-web-core/

# list python packages
ls -lh /pip
