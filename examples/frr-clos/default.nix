# BGP unnumbered and EVPN on NixOS: a CLOS fabric of FRR switches, each a
# small UML guest. Numbers follow NVIDIA's Cumulus Linux reference design:
# leaves 6510x, spines 65199, loopbacks 10.10.10.x, VRF RED on L3 VNI 4001.
#
#   nix build --file . frr-clos
#   nix run --file . frr-clos.driver -- --out ./out --break l2
{
  mkTest,
  lib,
  pkgs,
}:
let
  clos = import ./clos.nix { inherit lib pkgs; };

  topology = {
    spines = {
      spine01 = {
        asn = 65199;
        loopback = "10.10.10.101";
      };
      spine02 = {
        asn = 65199;
        loopback = "10.10.10.102";
      };
    };
    leaves = {
      leaf01 = {
        asn = 65101;
        loopback = "10.10.10.1";
      };
      leaf02 = {
        asn = 65102;
        loopback = "10.10.10.2";
      };
    };
    vrfs.RED.vni = 4001;
    vlans = {
      "10" = {
        vni = 10;
        vrf = "RED";
        gateway = "10.1.10.1/24";
      };
      "20" = {
        vni = 20;
        vrf = "RED";
        gateway = "10.1.20.1/24";
      };
    };
    # Every tenant subnet: what a server routes through its gateway.
    overlay = "10.1.0.0/16";
    servers = {
      server01 = {
        leaf = "leaf01";
        vlan = "10";
        address = "10.1.10.101/24";
      };
      server02 = {
        leaf = "leaf02";
        vlan = "10";
        address = "10.1.10.102/24";
      };
      server03 = {
        leaf = "leaf02";
        vlan = "20";
        address = "10.1.20.103/24";
      };
    };
  };
in
mkTest {
  name = "frr-clos";
  pythonPath = [ ./lib ];
  settings.clos = clos.settings topology;
  nodes = clos.nodes topology;
  phases = {
    underlay = {
      script = ./phases/underlay.py;
      after = [ "boot" ];
      description = "every loopback reaches every other, over two spines";
    };
    l2 = {
      script = ./phases/l2.py;
      after = [ "underlay" ];
      description = "VLAN 10 stretched across leaves on VNI 10";
    };
    l3 = {
      script = ./phases/l3.py;
      after = [ "l2" ];
      description = "VLAN 10 to VLAN 20 routed in VRF RED on L3 VNI 4001";
    };
    failover = {
      script = ./phases/failover.py;
      after = [ "l3" ];
      description = "a spine link down, then both";
    };
  };
}
