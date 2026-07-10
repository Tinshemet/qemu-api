/*
 * battery.dsl — synthetic Control-Method Battery + AC adapter for stealth
 * laptop personas. QEMU has no battery device, so a laptop persona otherwise
 * exposes no /sys/class/power_supply/BAT0 — a clean "this laptop has no
 * battery" inconsistency that upower/acpi/GNOME immediately reveal.
 *
 * Injected via `-acpitable file=battery.aml` only when cfg.battery is set
 * (laptop machine_class). Reports a full ~50 Wh Li-ion pack on AC power.
 * The pack maker is "SMP" (Simplo) — a real OEM battery supplier used across
 * Dell / Lenovo / HP / etc. — so the battery stays coherent regardless of the
 * machine's brand (real battery packs are not branded by the laptop vendor).
 *
 * Compile:  iasl -tc battery.dsl   →   battery.aml
 * (acpica-tools; added to complementary/install_executor.sh)
 */
DefinitionBlock ("battery.aml", "SSDT", 2, "NBOOK ", "BATTERY ", 0x00000001)
{
    Scope (\_SB)
    {
        Device (BAT0)
        {
            Name (_HID, EisaId ("PNP0C0A"))   // Control Method Battery
            Name (_UID, Zero)

            Method (_STA, 0, NotSerialized)
            {
                Return (0x1F)   // present + functioning + battery present
            }

            Method (_BIF, 0, NotSerialized)
            {
                Return (Package (0x0D)
                {
                    Zero,            // Power Unit: 0 = mWh / mW
                    0x0000C350,      // Design Capacity        = 50000 mWh
                    0x0000C350,      // Last Full Charge Cap   = 50000 mWh
                    One,             // Battery Technology: 1 = rechargeable
                    0x00002C88,      // Design Voltage         = 11400 mV
                    0x00001388,      // Warning capacity (10%) = 5000 mWh
                    0x000009C4,      // Low capacity (5%)      = 2500 mWh
                    One,             // Capacity granularity 1
                    One,             // Capacity granularity 2
                    "Primary",       // Model number (generic — pack, not vendor)
                    "3S1P",          // Serial number
                    "LION",          // Battery type
                    "SMP"            // OEM info (pack maker: Simplo)
                })
            }

            Method (_BST, 0, NotSerialized)
            {
                Return (Package (0x04)
                {
                    Zero,            // State: 0 = not charging (full, on AC)
                    0x00000000,      // Present rate (0 = not discharging)
                    0x0000C350,      // Remaining capacity = 50000 mWh (full)
                    0x00002C88       // Present voltage = 11400 mV
                })
            }
        }

        Device (ADP1)
        {
            Name (_HID, "ACPI0003")   // AC adapter
            Method (_STA, 0, NotSerialized) { Return (0x0F) }
            Method (_PSR, 0, NotSerialized) { Return (One) }   // AC online
            Name (_PCL, Package (0x01) { \_SB })
        }
    }
}
