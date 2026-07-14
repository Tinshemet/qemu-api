"""
autoinstall/ — all unattended-OS-install logic and answer-file templates live here.

  windows.py            Windows answer-file (autounattend.xml) generation.
  linux.py               Linux autoinstall/preseed generation (casper + debian-installer).
  templates/             The actual answer-file templates, edited directly:
    autounattend.xml.template    Windows Setup answer file.
    ubuntu-user-data.yaml.template   cloud-init autoinstall YAML (casper family:
                                     Ubuntu, Mint, and any future casper-based distro).
    kali-preseed-extra.cfg.template  debian-installer preseed additions (Kali and any
                                     future classic-d-i-based distro).

To add support for a new distro:
  - Same installer family as an existing entry (casper or debian-installer)? Just add
    a new entry to `unattended_linux` in ../config.json with its kernel_path/initrd_path
    (see the ISO under /casper or /install.amd) — the existing template already applies.
  - A genuinely different installer/answer-file format? Add a new template file here and
    a branch in linux.py that reads it, the same way the two existing families work.
"""
