// ADR-2610062000 P6. Record every outbound TCP connection and DNS lookup this node process makes,
// one JSON line each, to $NET_AUDIT_LOG. Loaded with NODE_OPTIONS=--require.
// Complete for the process, not sampled: fetch (undici), ws and tls all go
// through net.Socket#connect, and every name resolution through dns.lookup.
const fs = require("node:fs"), net = require("node:net"), dns = require("node:dns");
const out = process.env.NET_AUDIT_LOG;
const rec = (o) => { try { fs.appendFileSync(out, JSON.stringify({t: Date.now(), pid: process.pid, ...o}) + "\n"); } catch (_) {} };
const conn = net.Socket.prototype.connect;
net.Socket.prototype.connect = function (...args) {
  let a = args[0]; if (Array.isArray(a)) a = a[0];
  const o = (a && typeof a === "object") ? a : {port: args[0], host: args[1]};
  rec({lang: "node", ev: "connect", host: o.host || "localhost", port: o.port, path: o.path});
  return conn.apply(this, args);
};
const lookup = dns.lookup;
dns.lookup = function (host, ...rest) { rec({lang: "node", ev: "dns", host}); return lookup.call(this, host, ...rest); };
for (const fn of ["resolve", "resolve4", "resolve6"]) {
  const f = dns[fn]; if (f) dns[fn] = function (host, ...rest) { rec({lang: "node", ev: "dns", host, fn}); return f.call(this, host, ...rest); };
}
