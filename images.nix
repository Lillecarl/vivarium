/**
  Other distributions' cloud images, for `vivarium.image`.

  Each pins one build: openSUSE removes Tumbleweed snapshots within
  weeks, and a Leap build stays until the next maintenance image. The
  hash is the one openSUSE publishes beside the image.
*/
{ fetchurl }:
{
  /**
    openSUSE Leap 16.0, the community build of SLE 16. Root is XFS on the
    third partition.

    Masked under UML, each measured on this image:
    - `jeos-firstboot`, an interactive wizard that loops on the console.
    - `kbdsettings`: UML has no keyboard, and `setleds` fails the unit.
  */
  opensuse-leap-16_0 = {
    disk = fetchurl {
      url = "https://download.opensuse.org/distribution/leap/16.0/appliances/Leap-16.0-Minimal-VM.x86_64-Cloud-Build18.71.qcow2";
      hash = "sha256-wabFDFGkRxmUZibF0+oiuETx6DHmq/XzH0Y8ZaJqb9w=";
    };
    partition = 3;
    kernelParams = [
      "systemd.mask=jeos-firstboot.service"
      "systemd.mask=kbdsettings.service"
    ];
  };
}
