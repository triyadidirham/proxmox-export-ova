#!/usr/bin/env python3
"""Export stopped Proxmox QEMU VMs into portable, single-file OVA archives.

Uses qm, pvesm, and qemu-img available on the Proxmox host. Creates OVF 1.0,
VMDK streamOptimized images, optional SHA256 manifest, and uncompressed TAR OVA.
Does not modify source disks. Run as root on a Proxmox node.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import xml.etree.ElementTree as ET

OVF = 'http://schemas.dmtf.org/ovf/envelope/1'
RASD = 'http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_ResourceAllocationSettingData'
VSSD = 'http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/CIM_VirtualSystemSettingData'
VMW = 'http://www.vmware.com/schema/ovf'
OUTPUT_DIR = Path(__file__).resolve().parent / 'output'
ET.register_namespace('', OVF)
ET.register_namespace('ovf', OVF)
ET.register_namespace('rasd', RASD)
ET.register_namespace('vssd', VSSD)
ET.register_namespace('vmw', VMW)

def run(*args, capture=True):
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE if capture else None, check=True)
    return result.stdout if capture else ''

def element(parent, tag, text=None, attrs=None):
    if not tag.startswith('{'): tag = f'{{{OVF}}}{tag}'
    e = ET.SubElement(parent, tag, attrs or {})
    if text is not None: e.text = str(text)
    return e

def ovfattr(name): return f'{{{OVF}}}{name}'
def rasd(name): return f'{{{RASD}}}{name}'
def vssd(name): return f'{{{VSSD}}}{name}'
def vmw(name): return f'{{{VMW}}}{name}'

def add_item(section, fields):
    item = element(section, 'Item')
    for name, value in fields.items(): element(item, rasd(name), value)

def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b''): h.update(chunk)
    return h.hexdigest()

def img_info(path): return json.loads(run('qemu-img', 'info', '--output=json', str(path)))

def parse_config(vmid):
    raw = run('qm', 'config', str(vmid))
    return dict(line.split(': ', 1) for line in raw.splitlines() if ': ' in line)

def is_disk(v):
    return bool(re.fullmatch(r'(scsi|virtio|sata|ide)\d+', v))

def boot_order(cfg):
    m = re.search(r'(?:^|,)order=([^,]+)', cfg.get('boot', ''))
    if m: return m.group(1).split(';')
    return [cfg['bootdisk']] if 'bootdisk' in cfg else []

def disk_sort_key(key, order):
    bus = key.rstrip('0123456789')
    return (order.index(key) if key in order else len(order), bus, int(key[len(bus):]))

def disk_from_setting(setting):
    volume = setting.split(',', 1)[0]
    if volume in ('none', 'cdrom') or 'media=cdrom' in setting or volume.endswith(':cloudinit'):
        return None
    return volume

def resolve_volume(volume):
    p = Path(volume)
    if p.is_file() or p.is_block_device(): return str(p)
    return run('pvesm', 'path', volume).strip()

def good_name(value):
    name = re.sub(r'[^A-Za-z0-9_.-]+', '-', value).strip('.-_')
    return name[:90] or 'virtual-machine'

def check_tools():
    for program in ('qm', 'pvesm', 'qemu-img'):
        if not shutil.which(program): raise RuntimeError(f'Missing command: {program}. Run this script on a Proxmox VE host.')

def make_ovf(folder, name, disks, cpu, ram, os_name, nic_count, disk_controller, uefi=False, secure_boot=False):
    root = ET.Element(f'{{{OVF}}}Envelope', {ovfattr('version'): '1.0'})
    refs = element(root, 'References')
    dsec = element(root, 'DiskSection'); element(dsec, 'Info', 'Virtual disk information')
    for idx, disk in enumerate(disks, 1):
        fname = disk['file'].name
        element(refs, 'File', attrs={ovfattr('id'):f'file{idx}',ovfattr('href'):fname,ovfattr('size'):str(disk['file'].stat().st_size)})
        element(dsec, 'Disk', attrs={ovfattr('diskId'):f'disk{idx}',ovfattr('fileRef'):f'file{idx}',ovfattr('capacity'):str(disk['capacity']), ovfattr('capacityAllocationUnits'):'byte',ovfattr('format'):'http://www.vmware.com/interfaces/specifications/vmdk.html#streamOptimized'})
    if nic_count:
        nsec = element(root, 'NetworkSection');element(nsec, 'Info', 'Virtual network(s)')
        element(nsec, 'Network', attrs={ovfattr('name'):'VM Network'})
    vm = element(root,'VirtualSystem',attrs={ovfattr('id'):name})
    element(vm,'Info','Exported from Proxmox VE');element(vm,'Name',name)
    ossec=element(vm,'OperatingSystemSection',attrs={ovfattr('id'):'102'});element(ossec,'Info',f'Guest OS: {os_name}')
    hw=element(vm,'VirtualHardwareSection');element(hw,'Info','Virtual hardware')
    system=element(hw,'System');element(system,vssd('ElementName'),'Virtual Hardware Family')
    element(system,vssd('InstanceID'),'0');element(system,vssd('VirtualSystemIdentifier'),name)
    element(system,vssd('VirtualSystemType'),'vmx-13')
    add_item(hw,{'ElementName':f'{cpu} virtual CPU(s)','InstanceID':1,'ResourceType':3,'VirtualQuantity':cpu})
    add_item(hw,{'ElementName':f'{ram} MB RAM','InstanceID':2,'ResourceType':4,'AllocationUnits':'byte * 2^20','VirtualQuantity':ram})
    add_item(hw,{'ElementName':'SCSI Controller','InstanceID':3,'ResourceType':6,'ResourceSubType':disk_controller,'Address':0})
    for idx, disk in enumerate(disks,1):
        add_item(hw,{'ElementName':f'Hard Disk {idx}','InstanceID':10+idx,'ResourceType':17,'HostResource':f'ovf:/disk/disk{idx}','Parent':3,'AddressOnParent':idx-1})
    for idx in range(nic_count):
        add_item(hw,{'ElementName':f'Ethernet {idx+1}','InstanceID':50+idx,'ResourceType':10,'ResourceSubType':'E1000','AutomaticAllocation':'true','Connection':'VM Network'})
    if uefi:
        # VMware-style firmware hint; optional so other importers can ignore it.
        element(hw,vmw('Config'),attrs={ovfattr('required'):'false',vmw('key'):'firmware',vmw('value'):'efi'})
        if secure_boot: element(hw,vmw('Config'),attrs={ovfattr('required'):'false',vmw('key'):'bootOptions.efiSecureBootEnabled',vmw('value'):'true'})
    path=folder/f'{name}.ovf'
    ET.indent(ET.ElementTree(root),space='  ')
    ET.ElementTree(root).write(path,encoding='utf-8',xml_declaration=True)
    ET.parse(path)
    return path

def main():
    ap=argparse.ArgumentParser(
        prog='proxmox_ova_exporter.py',
        description='Export VM Proxmox yang sudah berhenti menjadi satu paket OVA (OVF + VMDK).',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=r"""PERSYARATAN:
  * Jalankan sebagai root pada host Proxmox VE (tersedia qm, pvesm, qemu-img).
  * VM harus stopped; siapkan ruang disk untuk VMDK sementara + OVA.
  * Bersihkan password/token, identitas unik, IP statis sebelum distribusi.

CONTOH:
  # Bantuan lengkap
  python3 proxmox_ova_exporter.py --help

  # Export VM 100, otomatis membaca nama, CPU, RAM, disk dari Proxmox
  # Hasil default: output/<nama-vm>-100.ova (folder output/ di sebelah script)
  python3 proxmox_ova_exporter.py 100

  # Nama custom + lokasi output + deskripsi OS (opsional)
  python3 proxmox_ova_exporter.py 100 --name debian13-base \
    --os 'Debian 13' --output /root/debian13-base-v1.0.ova

  # Gunakan VMDK streamOptimized yang sudah dikonversi; tanpa konversi ulang
  python3 proxmox_ova_exporter.py 100 \
    --disk /root/my-appliance-disk1.vmdk \
    --output /root/my-appliance-v1.0.ova

  # Multi-disk: ulangi --disk sesuai urutan disk guest
  python3 proxmox_ova_exporter.py 100 \
    --disk /root/os.vmdk --disk /root/data.vmdk \
    --output /root/appliance-multidisk.ova

  # Tanpa manifest; simpan file kerja untuk pemeriksaan
  python3 proxmox_ova_exporter.py 100 --no-manifest --keep-workdir

SETELAH EXPORT:
  tar -tf /root/debian13-base-v1.0.ova
  mkdir -p /root/ova-import && cd /root/ova-import
  tar -xf /root/debian13-base-v1.0.ova
  qm importovf 200 debian13-base.ovf local-lvm
  qm config 200  # verifikasi BIOS/UEFI, boot disk, controller, bridge

BIOS / UEFI:
  BIOS (seabios): OVF tanpa penanda firmware, importer memakai BIOS.
  UEFI (ovmf): OVF diberi penanda firmware=efi (dibaca VMware). Di Proxmox
  tujuan tetap jalankan: qm set 200 --bios ovmf --efidisk0 local-lvm:1,efitype=4m
  Isi NVRAM tidak ikut; guest wajib punya \EFI\BOOT\BOOTX64.EFI
  (Debian/Ubuntu: grub-install --removable sebelum export).
  Disk pertama di OVA = disk boot dari 'boot: order=' / 'bootdisk:'.

CATATAN:
  --os hanya deskripsi teks metadata OVF, bukan deteksi OS/driver.
  Source VM tidak dimodifikasi. Berkas output yang sudah ada tidak ditimpa.
  EFI/NVRAM, TPM, passthrough, cloud-init setup, NIC, IP dan opsi khusus
  Proxmox tidak otomatis dipindahkan. Uji boot & jaringan setelah import.
  Jangan gunakan contoh nama OVF pada import jika nama export berbeda.
"""
    )
    ap.add_argument('vmid', type=int, help='VM ID sumber pada Proxmox (wajib)')
    ap.add_argument('--name', help='Appliance name (default: Proxmox VM name)')
    ap.add_argument('--output', type=Path, help=f'Destination .ova path; default: {OUTPUT_DIR}/<name>-<vmid>.ova')
    ap.add_argument('--disk', type=Path, action='append', help='Already converted standalone streamOptimized VMDK; repeat for multiple disks (disables auto conversion)')
    ap.add_argument('--os', default='Other 64-bit', help='Deskripsi OS di OVF (default: Other 64-bit; tidak mengubah isi guest)')
    ap.add_argument('--controller', choices=['lsilogic','buslogic','pvscsi'],default='lsilogic',help='SCSI controller advertised in OVF (default: lsilogic)')
    ap.add_argument('--no-manifest', action='store_true',help='Omit optional SHA256 manifest')
    ap.add_argument('--keep-workdir', action='store_true',help='Keep intermediate OVF/VMDK files next to OVA in <name>-work directory')
    args=ap.parse_args()
    check_tools()
    if os.geteuid()!=0: raise RuntimeError('Run as root on the Proxmox host.')
    cfg=parse_config(args.vmid)
    status=run('qm','status',str(args.vmid)).strip()
    if 'stopped' not in status: raise RuntimeError(f'VM must be stopped before export (currently: {status}).')
    if cfg.get('lock'): raise RuntimeError(f'VM is locked: {cfg["lock"]}')
    name=good_name(args.name or cfg.get('name',f'vm-{args.vmid}'))
    output=(args.output or OUTPUT_DIR/f'{name}-{args.vmid}.ova').expanduser().resolve()
    if output.suffix.lower()!='.ova': raise ValueError('--output must end with .ova')
    output.parent.mkdir(parents=True,exist_ok=True)
    if output.exists(): raise FileExistsError(f'Output exists: {output}. Move/rename it rather than overwrite.')
    cpu=max(1,int(cfg.get('cores','1')))*max(1,int(cfg.get('sockets','1')))
    ram=int(cfg.get('memory','2048'))
    nets=sorted((k for k in cfg if re.fullmatch('net[0-9]+',k)),key=lambda k:int(k[3:]))
    source_disks=[]
    order=boot_order(cfg)
    for key in sorted((k for k in cfg if is_disk(k)),key=lambda k:disk_sort_key(k,order)):
        vol=disk_from_setting(cfg[key])
        if vol: source_disks.append((key,vol))
    uefi=cfg.get('bios','seabios')=='ovmf'
    secure_boot=uefi and 'pre-enrolled-keys=1' in cfg.get('efidisk0','')
    if uefi: print('WARNING: UEFI source VM; OVF is marked EFI but NVRAM boot entries are not transported. Guest needs fallback loader \\EFI\\BOOT\\BOOTX64.EFI.',file=sys.stderr)
    if 'tpmstate0' in cfg: print('WARNING: TPM state is NOT exported; Windows/BitLocker may require recovery.',file=sys.stderr)
    if 'efidisk0' in cfg: print('NOTE: EFI variable disk is intentionally excluded.',file=sys.stderr)
    if 'hookscript' in cfg: print('WARNING: Proxmox hookscript is NOT exported.',file=sys.stderr)
    if not args.disk and not source_disks: raise RuntimeError('No supported OS disks found in qm config.')
    if args.disk and len(args.disk)!=len(source_disks): print(f'WARNING: supplied {len(args.disk)} VMDK(s), source VM has {len(source_disks)} disk(s). Check that nothing is missing.',file=sys.stderr)
    if any(k.startswith('virtio') or k.startswith('sata') or k.startswith('ide') for k,_ in source_disks): print('WARNING: OVF maps all disk images to LSI SCSI; verify guest drivers and boot settings after import.',file=sys.stderr)
    print(f'VM {args.vmid} ({name}): {cpu} vCPU, {ram} MB, {"UEFI" if uefi else "BIOS"}, {len(source_disks)} source disk(s), {len(nets)} NIC(s)',flush=True)
    if source_disks: print('Disk order: '+', '.join(f'disk{n}={slot}' for n,(slot,_) in enumerate(source_disks,1)),flush=True)
    with tempfile.TemporaryDirectory(prefix='proxmox-ova-') as td:
        work=Path(td); disks=[]
        if args.disk:
            for n, source in enumerate(args.disk,1):
                source=source.expanduser().resolve()
                info=img_info(source)
                if info.get('format')!='vmdk':raise ValueError(f'Not VMDK: {source}')
                if info.get('backing-filename'):raise ValueError(f'VMDK has backing file: {source}')
                if info.get('format-specific',{}).get('data',{}).get('create-type') not in ('streamOptimized',):
                    raise ValueError(f'VMDK is not streamOptimized: {source}')
                dest=work/f'{name}-disk{n}.vmdk'
                os.symlink(source,dest)
                disks.append({'file':dest,'capacity':info['virtual-size']})
        else:
            for n,(slot,vol) in enumerate(source_disks,1):
                source=resolve_volume(vol)
                info=img_info(source)
                if info.get('backing-filename'): raise RuntimeError(f'{slot} has a backing file; flatten/resolve chain first: {source}')
                dest=work/f'{name}-disk{n}.vmdk'
                print(f'Converting {slot}: {vol} -> {dest.name}',flush=True)
                run('qemu-img','convert','-p','-O','vmdk','-o',f'adapter_type={args.controller},subformat=streamOptimized,compat6',source,str(dest),capture=False)
                disks.append({'file':dest,'capacity':img_info(dest)['virtual-size']})
        ovf=make_ovf(work,name,disks,cpu,ram,args.os,len(nets),args.controller,uefi,secure_boot)
        items=[ovf]
        if not args.no_manifest:
            mf=work/f'{name}.mf'
            with open(mf,'w',encoding='utf-8') as f:
                for file in [ovf]+[d['file'] for d in disks]:f.write(f'SHA256({file.name})= {sha256(file)}\n')
            items.append(mf)
        items.extend(d['file'] for d in disks)
        print(f'Packaging {output}',flush=True)
        with tarfile.open(output,'w',format=tarfile.GNU_FORMAT,dereference=True) as tar:
            for file in items:tar.add(file,arcname=file.name,recursive=False)
        with tarfile.open(output,'r') as tar:
            names=tar.getnames()
            assert names==[p.name for p in items], 'Unexpected OVA contents'
        if args.keep_workdir:
            dest=output.parent/f'{name}-{args.vmid}-work'
            if dest.exists():raise FileExistsError(dest)
            dest.mkdir()
            for f in items:shutil.copyfile(f,dest/f.name)
        print(f'SUCCESS: {output} ({output.stat().st_size/1024**3:.2f} GiB)')
        print('NOTE: OVA archives VM disks and generic virtual hardware, NOT host-specific bridges, IPs, secrets, EFI/TPM state or Proxmox-specific options.')
        print('Test import on a separate Proxmox node and verify guest boot/network before distribution.')
        print('After "qm importovf <VMID> ...", run on the destination:')
        if uefi: print(f'  qm set <VMID> --bios ovmf --efidisk0 <storage>:1,efitype=4m,pre-enrolled-keys={int(secure_boot)}')
        if nets: print('  qm set <VMID> --net0 virtio,bridge=vmbr0   # repeat per NIC; importovf does not create NICs')
        print('  qm set <VMID> --boot order=scsi0')

if __name__=='__main__':
    try:main()
    except (RuntimeError, ValueError, FileNotFoundError, subprocess.CalledProcessError, OSError) as e:
        print(f'ERROR: {e}',file=sys.stderr);sys.exit(1)
