# A spine-and-leaf (CLOS) fabric as vivarium nodes, after the reference
# design in NVIDIA's Cumulus Linux docs: eBGP unnumbered between every
# leaf and every spine, a /32 loopback per switch, and EVPN over VXLAN on
# the leaves -- one L2 VNI per VLAN, and one L3 VNI per VRF for symmetric
# routing. The topology is data; this turns it into guests.
#
# Interfaces are named after what is at the other end: on a leaf,
# `spine01` is its link to spine01 and `server01` its port to server01.
{ lib, pkgs }:
let
  bare = cidr: lib.head (lib.splitString "/" cidr);
  prefixOf = cidr: lib.toIntBase10 (lib.elemAt (lib.splitString "/" cidr) 1);

  # The segment between two guests: one name, whichever end asks.
  link = a: b: "${a}-${b}";

  # Below every segment's 9000, so a full server frame fits a VXLAN packet.
  serverMtu = 8950;

  switch =
    {
      name,
      asn,
      loopback,
      peers,
      extraBgp ? "",
      extraFrr ? "",
    }:
    {
      vivarium.memory = "256M";
      vivarium.mtu = 9000;
      environment.systemPackages = [ pkgs.nftables ];
      boot.kernel.sysctl = {
        "net.ipv4.ip_forward" = 1;
        "net.ipv6.conf.all.forwarding" = 1;
      };
      networking.interfaces.lo.ipv4.addresses = [
        {
          address = loopback;
          prefixLength = 32;
        }
      ];
      # FRR sends router advertisements on every unnumbered peer link; a
      # switch that took them would install a default route via its peer.
      systemd.network.networks = lib.listToAttrs (
        map (
          peer:
          lib.nameValuePair "40-${peer}" {
            networkConfig.IPv6AcceptRA = false;
          }
        ) peers
      );
      services.frr = {
        bgpd.enable = true;
        # `network` and never `redistribute connected` in the default VRF:
        # vec0, the uplink to the host, is connected too.
        config = ''
          frr defaults datacenter
          log syslog informational
          ${extraFrr}
          router bgp ${toString asn}
           bgp router-id ${loopback}
           bgp bestpath as-path multipath-relax
           neighbor fabric peer-group
           neighbor fabric remote-as external
          ${lib.concatMapStrings (peer: " neighbor ${peer} interface peer-group fabric\n") peers}
           address-family ipv4 unicast
            network ${loopback}/32
           exit-address-family
           address-family l2vpn evpn
            neighbor fabric activate
          ${extraBgp}
           exit-address-family
          exit
        '';
      };
    };

  nodes =
    topology:
    let
      inherit (topology)
        spines
        leaves
        vlans
        vrfs
        servers
        ;
      gatewayMac = topology.gatewayMac or "44:38:39:ff:00:01";
      serversOn = leaf: lib.filterAttrs (_: server: server.leaf == leaf) servers;
      vrfTable = name: 1000 + lib.lists.findFirstIndex (other: other == name) 0 (lib.attrNames vrfs);

      spineNode = name: spine: {
        vivarium.interfaces = lib.mapAttrs (leaf: _: { segment = link leaf name; }) leaves;
        imports = [
          (switch {
            inherit name;
            inherit (spine) asn loopback;
            peers = lib.attrNames leaves;
          })
        ];
      };

      leafNode =
        name: leaf:
        let
          vni = id: "vni${toString id}";
        in
        {
          vivarium.interfaces =
            lib.mapAttrs (spine: _: { segment = link name spine; }) spines
            // lib.mapAttrs (server: _: { segment = link name server; }) (serversOn name);
          imports = [
            (switch {
              inherit name;
              inherit (leaf) asn loopback;
              peers = lib.attrNames spines;
              extraFrr = lib.concatStrings (
                lib.mapAttrsToList (vrf: tenant: ''
                  vrf ${vrf}
                   vni ${toString tenant.vni}
                  exit-vrf
                '') vrfs
              );
              extraBgp = "  advertise-all-vni";
            })
          ];
          services.frr.config = lib.mkAfter (
            lib.concatStrings (
              lib.mapAttrsToList (vrf: _: ''
                router bgp ${toString leaf.asn} vrf ${vrf}
                 bgp router-id ${leaf.loopback}
                 address-family ipv4 unicast
                  redistribute connected
                 exit-address-family
                 address-family l2vpn evpn
                  advertise ipv4 unicast
                 exit-address-family
                exit
              '') vrfs
            )
          );

          # One bridge per VNI, the VXLAN device its only fabric port: an
          # L2 VNI per VLAN, with the anycast gateway on its bridge, and an
          # L3 VNI per VRF, with no address, for symmetric routing.
          systemd.network.netdevs =
            lib.mapAttrs' (
              vrf: tenant:
              lib.nameValuePair "20-${vrf}" {
                netdevConfig = {
                  Kind = "vrf";
                  Name = vrf;
                };
                vrfConfig.Table = vrfTable vrf;
              }
            ) vrfs
            // lib.listToAttrs (
              lib.concatLists (
                map
                  (
                    { id, gateway }:
                    [
                      (lib.nameValuePair "20-br${toString id}" {
                        netdevConfig = {
                          Kind = "bridge";
                          Name = "br${toString id}";
                        }
                        // lib.optionalAttrs gateway { MACAddress = gatewayMac; };
                      })
                      (lib.nameValuePair "20-${vni id}" {
                        netdevConfig = {
                          Kind = "vxlan";
                          Name = vni id;
                        };
                        vxlanConfig = {
                          VNI = id;
                          Local = leaf.loopback;
                          DestinationPort = 4789;
                          MacLearning = false;
                          Independent = true;
                        };
                      })
                    ]
                  )
                  (
                    lib.mapAttrsToList (_: vlan: {
                      id = vlan.vni;
                      gateway = true;
                    }) vlans
                    ++ lib.mapAttrsToList (_: tenant: {
                      id = tenant.vni;
                      gateway = false;
                    }) vrfs
                  )
              )
            );

          systemd.network.networks =
            let
              bridged = id: {
                matchConfig.Name = vni id;
                networkConfig = {
                  Bridge = "br${toString id}";
                  LinkLocalAddressing = "no";
                };
              };
            in
            lib.mapAttrs' (
              vrf: _:
              lib.nameValuePair "30-${vrf}" {
                matchConfig.Name = vrf;
                linkConfig.RequiredForOnline = "no";
              }
            ) vrfs
            // lib.listToAttrs (
              lib.concatLists (
                lib.mapAttrsToList (_: vlan: [
                  (lib.nameValuePair "30-${vni vlan.vni}" (bridged vlan.vni))
                  (lib.nameValuePair "30-br${toString vlan.vni}" {
                    matchConfig.Name = "br${toString vlan.vni}";
                    address = [ vlan.gateway ];
                    networkConfig = {
                      VRF = vlan.vrf;
                      ConfigureWithoutCarrier = true;
                      LinkLocalAddressing = "no";
                    };
                  })
                ]) vlans
                ++ lib.mapAttrsToList (vrf: tenant: [
                  (lib.nameValuePair "30-${vni tenant.vni}" (bridged tenant.vni))
                  (lib.nameValuePair "30-br${toString tenant.vni}" {
                    matchConfig.Name = "br${toString tenant.vni}";
                    networkConfig = {
                      VRF = vrf;
                      ConfigureWithoutCarrier = true;
                      LinkLocalAddressing = "no";
                    };
                  })
                ]) vrfs
              )
            )
            # Each server port joins its VLAN's bridge.
            // lib.mapAttrs' (
              server: attached:
              lib.nameValuePair "40-${server}" {
                networkConfig = {
                  Bridge = "br${toString vlans.${attached.vlan}.vni}";
                  LinkLocalAddressing = "no";
                };
              }
            ) (serversOn name);
        };

      serverNode =
        name: server:
        let
          vlan = vlans.${server.vlan};
        in
        {
          vivarium.memory = "192M";
          vivarium.mtu = 9000;
          vivarium.interfaces.uplink = {
            segment = link server.leaf name;
            addresses = [ server.address ];
          };
          # The overlay through the anycast gateway; vec0 keeps the default.
          networking.interfaces.uplink.ipv4.routes = [
            {
              address = bare topology.overlay;
              prefixLength = prefixOf topology.overlay;
              via = bare vlan.gateway;
            }
          ];
          networking.interfaces.uplink.mtu = serverMtu;
        };
    in
    lib.mapAttrs spineNode spines // lib.mapAttrs leafNode leaves // lib.mapAttrs serverNode servers;

  # What the phases need to know, without reading Nix.
  settings = topology: {
    gateways = lib.mapAttrs (_: vlan: bare vlan.gateway) topology.vlans;
    spines = lib.mapAttrs (_: spine: { inherit (spine) asn loopback; }) topology.spines;
    leaves = lib.mapAttrs (_: leaf: { inherit (leaf) asn loopback; }) topology.leaves;
    servers = lib.mapAttrs (_: server: {
      inherit (server) leaf vlan;
      address = bare server.address;
      vni = topology.vlans.${server.vlan}.vni;
    }) topology.servers;
  };
in
{
  inherit nodes settings;
}
