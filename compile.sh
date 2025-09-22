#!/bin/bash
set -e; set -o pipefail; set -m
[ "${DEBUG^^}" != 'TRUE' ] || set -x

# do it explicitely
set -x

# TODO: restore contrail-debug packages (see "debug" docker image)
# TODO: restore contrail-tor-agent

SCONS_OPT=${SCONS_OPT:-production}

my_dir=$(realpath $(dirname "$0"))
src_root=$(dirname $(dirname "$my_dir"))
buildroot="$src_root/buildroot"

src_ver=$(cat $src_root/controller/src/base/version.info)

# now we are creating "buildroot" - it will be used in later "docker build" as a "multistage"
rm -rf ${buildroot}/
mkdir -p ${buildroot}/
echo "$src_ver" > ${buildroot}/Version

mkdir -p ${buildroot}/usr/bin/tools ${buildroot}/opt/contrail/ddp
mkdir -p ${buildroot}/usr/bin ${buildroot}/usr/lib ${buildroot}/opt ${buildroot}/opt/python/opserver ${buildroot}/usr/bin/tools/
mkdir -p ${buildroot}/usr/share/lua/ ${buildroot}/usr/local/share/wireshark/ ${buildroot}/usr/local/lib64/wireshark/plugins/
mkdir -p ${buildroot}/usr/src/modules/
mkdir -p ${buildroot}/usr/src/contrail/contrail-web-controller/ ${buildroot}/usr/src/contrail/contrail-web-core/

pushd $src_root/
build_jobs=$(nproc --ignore=1)
# compile and pack most components
scons -j "$build_jobs" --opt=$SCONS_OPT --root=${buildroot} --without-dpdk install
# dpdk stuff
scons \
    --opt=$SCONS_OPT \
    -j "$build_jobs" \
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

# neutron plugin (outside of scons files as others)
pushd $src_root/openstack/neutron_plugin
python3 setup.py bdist_wheel --dist-dir /pip
popd

# heat plugin (outside of scons files as others)
pushd $src_root/openstack/contrail-heat
python3 setup.py bdist_wheel --dist-dir /pip
popd

# contrail-docs
# Move schema specific files to opserver
for mod_dir in ${buildroot}/usr/share/doc/contrail-docs/html/messages/* ; do
    if [[ ! -d $mod_dir ]]; then
        continue
    fi
    mkdir -p ${buildroot}/opt/python/opserver/stats_schema/$(basename $mod_dir)
    for statsfile in ${buildroot}/usr/share/doc/contrail-docs/html/messages/$(basename $mod_dir)/*_stats_tables.json ; do
        mv $statsfile ${buildroot}/opt/python/opserver/stats_schema/$(basename $mod_dir)/
    done
done
# Index files
python3 $src_root/tools/build/generate_doc_index.py ${buildroot}/usr/share/doc/contrail-docs/html/messages

# pack vrouter sources
pushd ${buildroot}
cd usr/src/vrouter
echo "$src_ver" > version
tar -czf ${buildroot}/usr/src/modules/contrail-vrouter.tar.gz .
popd
rm -rf ${buildroot}/usr/src/vrouter

####################################################### dpdk stuff
cp $src_root/build/$SCONS_OPT/vrouter/dpdk/contrail-vrouter-dpdk ${buildroot}/usr/bin/contrail-vrouter-dpdk
cp $src_root/build/$SCONS_OPT/vrouter/dpdk/dpdk-devbind.py ${buildroot}/usr/bin/dpdk_nic_bind.py
cp $src_root/vrouter/dpdk/ddp/mplsogreudp.pkg ${buildroot}/opt/contrail/ddp/mplsogreudp.pkg
# tools
cp $src_root//build/$SCONS_OPT/vrouter/dpdk/x86_64-native-linuxapp-gcc/app/testpmd ${buildroot}/usr/bin/tools/testpmd

N3KFLOW_DUMP_BINARY="$src_root/build/production/vrouter/dpdk/x86_64-native-linuxapp-gcc/build/app/n3kflow-dump/n3kflow-dump"
cp "$N3KFLOW_DUMP_BINARY" ${buildroot}/usr/bin/n3kflow-dump
cp $src_root/build/$SCONS_OPT/vrouter/dpdk/x86_64-native-linuxapp-gcc/app/n3k-info ${buildroot}/usr/bin/n3k-info
####################################################### dpdk stuff

# contrail-manifest package
cp $src_root/.repo/manifest.xml ${buildroot}/manifest.xml

# opts section
# TODO: change path in SConscript
mv ${buildroot}/usr/bin/fabric_ansible_playbooks-0.1.dev0.tar.gz ${buildroot}/opt/

# for unknown reason most libs are aout of buildroot
cp -a /root/work/build/lib/lib*.so* ${buildroot}/usr/lib/
# TODO: change path in SConscript
mv ${buildroot}/etc/contrail/dns/applynamedconfig.py ${buildroot}/usr/bin/

# TODO: change path in SConscript
mv ${buildroot}/usr/bin/vrouter-port-control ${buildroot}/usr/bin/vrouter-port-control.py

# vrouter tools
# TODO: change path in SConscript
mv ${buildroot}/usr/bin/dropstats ${buildroot}/usr/bin/tools/
mv ${buildroot}/usr/bin/flow ${buildroot}/usr/bin/tools/
mv ${buildroot}/usr/bin/mirror ${buildroot}/usr/bin/tools/
mv ${buildroot}/usr/bin/mpls ${buildroot}/usr/bin/tools/
mv ${buildroot}/usr/bin/nh ${buildroot}/usr/bin/tools/
mv ${buildroot}/usr/bin/rt ${buildroot}/usr/bin/tools/
mv ${buildroot}/usr/bin/vrfstats ${buildroot}/usr/bin/tools/
mv ${buildroot}/usr/bin/vrcli ${buildroot}/usr/bin/tools/
mv ${buildroot}/usr/bin/vxlan ${buildroot}/usr/bin/tools/
mv ${buildroot}/usr/bin/vrouter ${buildroot}/usr/bin/tools/
mv ${buildroot}/usr/bin/vrmemstats ${buildroot}/usr/bin/tools/
mv ${buildroot}/usr/bin/vifdump ${buildroot}/usr/bin/tools/
mv ${buildroot}/usr/bin/vrftable ${buildroot}/usr/bin/tools/
mv ${buildroot}/usr/bin/vrinfo ${buildroot}/usr/bin/tools/
mv ${buildroot}/usr/bin/dpdkinfo ${buildroot}/usr/bin/tools/
mv ${buildroot}/usr/bin/dpdkconf ${buildroot}/usr/bin/tools/
mv ${buildroot}/usr/bin/dpdkvifstats.py ${buildroot}/usr/bin/tools/
mv ${buildroot}/usr/bin/sandump ${buildroot}/usr/bin/tools/
mv ${buildroot}/usr/bin/pkt_droplog.py ${buildroot}/usr/bin/tools/

# TODO: change path in SConscript
mv ${buildroot}/usr/share/contrail/*.lua ${buildroot}/usr/local/lib64/wireshark/plugins/

# webui
cp -rp $src_root/contrail-web-controller/* ${buildroot}/usr/src/contrail/contrail-web-controller/
cp -rp $src_root/contrail-web-core/* ${buildroot}/usr/src/contrail/contrail-web-core/

ls -lR ${buildroot}/
