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
git clone https://github.com/triyadidirham/proxmox-export-ova.git
cd proxmox-export-ova
python3 proxmox_ova_exporter.py --help
```

Tanpa `--output`, hasil disimpan di folder `output/` di sebelah script dengan nama `<nama-vm>-<vmid>.ova` (folder dibuat otomatis). Folder ini ada di `.gitignore`, jadi file OVA hasil export tidak ikut masuk ke repository. File kerja dari `--keep-workdir` juga disimpan di folder yang sama.

## Contoh export

```bash
# 1. Export standar (nama dari qm config) -> output/<nama-vm>-100.ova
qm shutdown 100
qm status 100
python3 proxmox_ova_exporter.py 100

# 2. Nama file sendiri dan teks deskripsi OS
python3 proxmox_ova_exporter.py 100 \
  --name debian13-base --os 'Debian 13' \
  --output /root/debian13-base-v1.0.ova

# 3. Memakai VMDK standalone streamOptimized yang sudah ada
python3 proxmox_ova_exporter.py 100 \
  --disk /root/my-appliance-disk1.vmdk \
  --output /root/my-appliance-v1.0.ova

# 4. VMDK lebih dari satu (urutan harus sesuai disk guest)
python3 proxmox_ova_exporter.py 100 \
  --disk /root/os.vmdk --disk /root/data.vmdk \
  --output /root/vm100-v1.0.ova

# 5. Debug: simpan disk/OVF sementara
python3 proxmox_ova_exporter.py 100 --keep-workdir
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

Ganti VM ID, nama OVF hasil export, dan storage `local-lvm`. `qm importovf` tidak membuat NIC, jadi tambahkan sendiri:

```bash
qm set 200 --net0 virtio,bridge=vmbr0
qm set 200 --boot order=scsi0
```

Periksa controller, BIOS/UEFI, boot order, disk, dan bridge sebelum `qm start 200`. Atur IP/hostname/credential unik pada guest, lalu tes boot dan konektivitas. Di akhir export, script juga mencetak perintah `qm set` yang sesuai untuk VM tersebut.

## BIOS dan UEFI

Script membaca `bios:` dari `qm config` dan menampilkan `BIOS` atau `UEFI` saat export.

**BIOS (`seabios`, default Proxmox)**: OVF tidak memuat penanda firmware, sehingga importer memakai BIOS. Tidak perlu langkah tambahan.

**UEFI (`ovmf`)**: OVF diberi penanda `vmw:Config firmware=efi` (dan `bootOptions.efiSecureBootEnabled` bila `efidisk0` memakai `pre-enrolled-keys=1`). VMware membaca penanda ini; di Proxmox firmware tetap harus diatur setelah import:

```bash
qm set 200 --bios ovmf --efidisk0 local-lvm:1,efitype=4m,pre-enrolled-keys=0
```

Isi `efidisk0` (NVRAM berisi boot entry) **tidak** ikut diekspor. VM tujuan hanya bisa boot jika guest punya bootloader fallback `\EFI\BOOT\BOOTX64.EFI`:

- Windows: sudah ada secara default.
- Debian/Ubuntu: jalankan di guest sebelum export, lalu matikan VM:

  ```bash
  grub-install --target=x86_64-efi --efi-directory=/boot/efi --removable
  # atau permanen:
  echo 'grub-efi-amd64 grub2/force_efi_extra_removable boolean true' | debconf-set-selections
  dpkg-reconfigure -fnoninteractive grub-efi-amd64
  ```

Jika VM tujuan masuk ke UEFI shell, bootloader fallback belum ada.

**Urutan disk**: disk pertama di OVA adalah disk boot sesuai `boot: order=` (atau `bootdisk:` pada config lama), sisanya diurutkan per bus. Urutan ditampilkan sebagai `Disk order: disk1=..., disk2=...` saat export.

### Contoh lengkap VM UEFI

```bash
# --- Di host sumber ---
# 1. (Linux guest) pastikan bootloader fallback ada, dari dalam VM 100:
#    grub-install --target=x86_64-efi --efi-directory=/boot/efi --removable
#    ls /boot/efi/EFI/BOOT/BOOTX64.EFI
qm shutdown 100
qm config 100 | grep -E '^(bios|efidisk0|boot):'   # bios: ovmf

# 2. Export; output menampilkan "UEFI" dan perintah qm set untuk tujuan
python3 proxmox_ova_exporter.py 100 --name debian13-uefi
# -> output/debian13-uefi-100.ova

# 3. Salin OVA ke host tujuan
scp output/debian13-uefi-100.ova root@<host-tujuan>:/root/

# --- Di host tujuan ---
mkdir -p /root/ova-import && cd /root/ova-import
tar -xf /root/debian13-uefi-100.ova
qm importovf 200 debian13-uefi.ovf local-lvm

# 4. Aktifkan UEFI + EFI disk baru, NIC, dan boot order
qm set 200 --bios ovmf --efidisk0 local-lvm:1,efitype=4m,pre-enrolled-keys=0
qm set 200 --net0 virtio,bridge=vmbr0
qm set 200 --boot order=scsi0
qm config 200
qm start 200
```

Gunakan `pre-enrolled-keys=1` hanya jika VM sumber memakai Secure Boot. Script mencetak nilai yang sesuai di akhir export.

## Keterbatasan

- Ini bukan proses live export; VM sumber harus berhenti.
- OVF memakai controller SCSI, serta NIC E1000 sebagai representasi generik. Hal tersebut tidak selalu setara hardware Proxmox aslinya.
- EFI NVRAM, TPM state, perangkat passthrough, NIC/bridge, opsi storage tertentu, dan Proxmox-specific options tidak otomatis diikutkan.
- OVA ditulis dengan format tar GNU agar disk VMDK di atas 8 GiB didukung.
- VM UEFI/Windows/BitLocker dan VM dengan beberapa disk mungkin memerlukan penyesuaian tambahan.
- Ini alat packaging, **bukan** sanitasi credential atau generator konfigurasi first boot tiap client.
- Validasi XML dan daftar archive bukan jaminan kompatibilitas. Wajib tes import pada host lain sebelum distribusi produksi.
