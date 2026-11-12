# Minimal DHCP-only dnsmasq image for the LAN front (built natively on the front host's architecture).
FROM alpine:3.21
RUN apk add --no-cache dnsmasq
ENTRYPOINT ["dnsmasq", "--keep-in-foreground", "--conf-file=/etc/dnsmasq-dhcp.conf"]
