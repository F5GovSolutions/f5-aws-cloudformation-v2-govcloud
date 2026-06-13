#!/usr/bin/env python3
# cluster-heal-trust.py <peer_ip> <peer_name>
# Helper for cluster-heal.sh step 4a. Fetches the BIG-IP admin password from AWS
# Secrets Manager (SigV4-signed GetSecretValue using the instance IAM role + the
# secret ARN in /config/cloud/secret_id), then POSTs /mgmt/tm/cm/add-to-trust to
# add the peer device to the local Root trust-domain. This is the call DO's own
# clustering never performs (its joiner deadlocks), and the one we proved works.
# stdlib only (no boto3/aws-cli on BIG-IP). Idempotent: re-running once trusted
# just returns "already part of a trust-domain".
import sys, json, urllib.request, hashlib, hmac, datetime, ssl, base64, time
peer_ip   = sys.argv[1] if len(sys.argv) > 1 else ''
peer_name = sys.argv[2] if len(sys.argv) > 2 else ''
MD = 'http://169.254.169.254'

# The instance metadata service can be briefly unreachable right after a reboot
# ("No route to host") while the dataplane/route comes up, so retry IMDS calls.
def _tok(tries=6):
    for i in range(tries):
        try:
            r = urllib.request.Request(MD + '/latest/api/token', method='PUT',
                                       headers={'X-aws-ec2-metadata-token-ttl-seconds': '21600'})
            return urllib.request.urlopen(r, timeout=5).read().decode()
        except Exception:
            if i == tries - 1:
                return None
            time.sleep(5)

def _md(path, tok, tries=6):
    h = {'X-aws-ec2-metadata-token': tok} if tok else {}
    for i in range(tries):
        try:
            return urllib.request.urlopen(urllib.request.Request(MD + path, headers=h), timeout=5).read().decode()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(5)

def _hmac(key, msg):
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()

def get_secret():
    tok = _tok()
    region = json.loads(_md('/latest/dynamic/instance-identity/document', tok))['region']
    role = _md('/latest/meta-data/iam/security-credentials/', tok).strip().splitlines()[0]
    c = json.loads(_md('/latest/meta-data/iam/security-credentials/' + role, tok))
    ak, sk, st = c['AccessKeyId'], c['SecretAccessKey'], c['Token']
    arn = open('/config/cloud/secret_id').read().strip()
    svc = 'secretsmanager'
    host = '%s.%s.amazonaws.com' % (svc, region)
    body = json.dumps({'SecretId': arn})
    tgt = 'secretsmanager.GetSecretValue'
    ct = 'application/x-amz-json-1.1'
    n = datetime.datetime.utcnow()
    ad = n.strftime('%Y%m%dT%H%M%SZ')
    ds = n.strftime('%Y%m%d')
    ph = hashlib.sha256(body.encode()).hexdigest()
    ch = 'content-type:%s\nhost:%s\nx-amz-date:%s\nx-amz-security-token:%s\nx-amz-target:%s\n' % (ct, host, ad, st, tgt)
    sh = 'content-type;host;x-amz-date;x-amz-security-token;x-amz-target'
    cr = 'POST\n/\n\n%s\n%s\n%s' % (ch, sh, ph)
    scope = '%s/%s/%s/aws4_request' % (ds, region, svc)
    sts = 'AWS4-HMAC-SHA256\n%s\n%s\n%s' % (ad, scope, hashlib.sha256(cr.encode()).hexdigest())
    kg = _hmac(_hmac(_hmac(_hmac(('AWS4' + sk).encode(), ds), region), svc), 'aws4_request')
    sig = hmac.new(kg, sts.encode(), hashlib.sha256).hexdigest()
    auth = 'AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s' % (ak, scope, sh, sig)
    req = urllib.request.Request('https://%s/' % host, data=body.encode(), method='POST', headers={
        'Content-Type': ct, 'X-Amz-Date': ad, 'X-Amz-Security-Token': st,
        'X-Amz-Target': tgt, 'Authorization': auth})
    return json.loads(urllib.request.urlopen(req, timeout=10).read().decode())['SecretString']

if not peer_ip:
    print('cluster-heal-trust: no peer_ip given'); sys.exit(1)
pw = get_secret()
ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
data = {'command': 'run', 'name': 'Root', 'caDevice': True, 'device': peer_ip,
        'username': 'admin', 'password': pw}
if peer_name:
    data['deviceName'] = peer_name
h = {'Content-Type': 'application/json',
     'Authorization': 'Basic ' + base64.b64encode(('admin:' + pw).encode()).decode()}
req = urllib.request.Request('https://localhost/mgmt/tm/cm/add-to-trust',
                             data=json.dumps(data).encode(), method='POST', headers=h)
try:
    out = urllib.request.urlopen(req, timeout=60, context=ctx).read().decode()
    print('add-to-trust device=%s name=%s OK: %s' % (peer_ip, peer_name, out[:120]))
except urllib.error.HTTPError as e:
    print('add-to-trust device=%s name=%s HTTP %s: %s' % (peer_ip, peer_name, e.code, e.read().decode()[:160]))
