# Proxmox OVA Exporter — Panduan Penggunaan

Script `proxmox_ova_exporter.py` membuat arsip OVA dari VM QEMU/KVM Proxmox VE yang **sudah dimatikan**. OVA memuat descriptor `.ovf`, disk `.vmdk` streamOptimized, dan manifest `.mf` (opsional).

## Persyaratan

- Jalankan sebagai `root` pada node Proxmox VE dengan `python3`, `qm`, `pvesm`, dan `qemu-img`.
- VM harus berstatus `stopped` dan tidak sedang dikunci.
- Siapkan ruang disk cukup untuk VMDK sementara dan OVA final; output tidak akan menimpa file yang sudah ada.
- Bersihkan identitas unik, rahasia, SSH host keys, konfigurasi IP spesifik, dan credential sebelum membagikan appliance.
- Pastikan seluruh disk yang diperlukan ikut diekspor; dukungan controller, boot firmware, dan NIC pada target harus diverifikasi.

## Instalasi dan help

```bash
cp proxmox_ova_exporter.py /root/
chmod +x /root/proxmox_ova_exporter.py
python3 /root/proxmox_ova_exporter.py --help
```

## Contoh export

```bash
# 1. Export standar (nama dari qm config)
qm shutdown 100
qm status 100
python3 /root/proxmox_ova_exporter.py 100

# 2. Nama file sendiri dan teks deskripsi OS
python3 /root/proxmox_ova_exporter.py 100 \
  --name debian13-base --os 'Debian 13' \
  --output /root/debian13-base-v1.0.ova

# 3. Memakai VMDK standalone streamOptimized yang sudah ada
python3 /root/proxmox_ova_exporter.py 100 \
  --disk /root/my-appliance-disk1.vmdk \
  --output /root/my-appliance-v1.0.ova

# 4. VMDK lebih dari satu (urutan harus sesuai disk guest)
python3 /root/proxmox_ova_exporter.py 100 \
  --disk /root/os.vmdk --disk /root/data.vmdk \
  --output /root/vm100-v1.0.ova

# 5. Debug: simpan disk/OVF sementara
python3 /root/proxmox_ova_exporter.py 100 --keep-workdir
```

**Parameter `--os` opsional:** hanya menambahkan deskripsi di OVF; bukan pendeteksi OS, bukan installer, dan bukan pemilih driver. Default-nya `Other 64-bit`.

## Memeriksa hasil

```bash
tar -tf /root/debian13-base-v1.0.ova
sha256sum /root/debian13-base-v1.0.ova
```

Simpan checksum beserta versi file di repository distribusi.

## Import di Proxmox client

```bash
mkdir -p /root/ova-import
cd /root/ova-import
tar -xf /root/debian13-base-v1.0.ova
qm importovf 200 debian13-base.ovf local-lvm
qm config 200
```

Ganti VM ID, nama OVF hasil export, dan storage `local-lvm`. Periksa controller, BIOS/UEFI, boot order, disk, dan bridge sebelum `qm start 200`. Atur IP/hostname/credential unik pada guest, lalu tes boot dan konektivitas.

## Keterbatasan

- Ini bukan proses live export; VM sumber harus berhenti.
- OVF memakai controller SCSI, serta NIC E1000 sebagai representasi generik. Hal tersebut tidak selalu setara hardware Proxmox aslinya.
- EFI NVRAM, TPM state, perangkat passthrough, opsi storage/bridge tertentu, dan Proxmox-specific options tidak otomatis diikutkan.
- VM UEFI/Windows/BitLocker dan VM dengan beberapa disk mungkin memerlukan penyesuaian tambahan.
- Ini alat packaging, **bukan** sanitasi credential atau generator konfigurasi first boot tiap client.
- Validasi XML dan daftar archive bukan jaminan kompatibilitas. Wajib tes import pada host lain sebelum distribusi produksi.
