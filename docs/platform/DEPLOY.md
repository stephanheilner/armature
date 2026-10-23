# Deploying the Armature server behind a reverse proxy

`armature server run` listens on loopback by default (`host: 127.0.0.1`,
`port: 8098`). Put a TLS-terminating reverse proxy on the same host in front of
it, and set `public_origin` to the URL people type, such as
`https://armature.example.com`.

## Client addresses

Sign-in throttles count failures per client address. Behind a proxy, every
request arrives from `127.0.0.1`, so without the real address one attacker's
failures lock out everyone.

The server reads the `X-Real-IP` header only when both of these hold:

- `trust_real_ip: true` is set in `server.yaml`.
- The request comes from a loopback address, which is the proxy on this host.

It never reads `X-Forwarded-For`. Set `trust_real_ip` only when a proxy on this
host always overwrites `X-Real-IP`, because otherwise any client can choose its
own address and slip past the throttle.

### nginx

```nginx
location / {
    proxy_pass http://127.0.0.1:8098;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
}
```

### Caddy

```caddy
armature.example.com {
    reverse_proxy 127.0.0.1:8098 {
        header_up X-Real-IP {remote_host}
    }
}
```

### Apache httpd

```apache
ProxyPass        / http://127.0.0.1:8098/
ProxyPassReverse / http://127.0.0.1:8098/
RequestHeader set X-Real-IP "%{REMOTE_ADDR}s"
```

Each of these overwrites any `X-Real-IP` the client sent.

## A base path

To serve the server under a path such as `/armature`, set
`base_path: /armature` and proxy that path without stripping it:

```nginx
location /armature/ {
    proxy_pass http://127.0.0.1:8098;
    proxy_set_header X-Real-IP $remote_addr;
}
```

## Headers the server sets

Every response carries a strict Content-Security-Policy, `X-Frame-Options:
DENY`, and `Cache-Control: no-store`. Unless `public_origin` is a plain-HTTP
localhost address, it also sends `Strict-Transport-Security:
max-age=31536000`, so serve the origin over HTTPS only.
