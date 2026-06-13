# Deploying the F5 BIG-IP Failover Solution in AWS GovCloud (US)

A step-by-step guide for deploying the `examples/failover` CloudFormation solution into **AWS GovCloud (US)** (`aws-us-gov` partition), including **air-gapped / no-internet-egress** environments.

This guide is written for a **first-time** operator. It assumes you can use the AWS Console and a terminal with the AWS CLI, but it does **not** assume prior F5 BIG-IP, Declarative Onboarding, or Cloud Failover Extension experience. The template defaults to values that are derived from environment. If a parameter is "Required" it will say so. For most parameters leaving the default is fine. These are described below.

> This is a standalone companion to the main [`README.md`](./README.md). The README is the authoritative parameter and architecture reference; this guide is the start-to-finish GovCloud walkthrough for a single HA setup with CFE. It has automated VPC Endpoints built-in, this has always been a requirement in air-gapped environments where internet egress is not allowed.

---

## 1. What you are deploying, and why GovCloud is different

The solution deploys an **active/standby pair of F5 BIG-IP Virtual Editions** across two Availability Zones, with **Cloud Failover Extension (CFE)** moving the floating application addresses (and routes) between them when one fails. Each BIG-IP self-configures at first boot using **F5 BIG-IP Runtime Init**, which installs and configures three F5 automation extensions, if you are not familiar with these extensions you can find them on the official F5 website: https://clouddocs.f5.com:

- **DO** (Declarative Onboarding) — base setup: hostname, VLANs, Self IPs, admin password, and clustering (device trust + sync-failover device group).
- **AS3** (Application Services) — the example application / virtual servers + WAF policy.
- **CFE** (Cloud Failover Extension) — moves the floating addresses/routes in AWS on failover.

The CloudFormation template is a **parent stack** that references several **nested stacks** (`modules/**`) and a set of **BIG-IP artifacts** (the runtime-init installer and the three extension RPMs). In a normal commercial deployment these are fetched from F5's public bucket and CDN over the internet.

**GovCloud changes two things:**

1. **GovCloud is a separate AWS partition** (`aws-us-gov`). Two *different* consumers pull from F5's "public bucket + CDN," and they fail for different reasons — keep them separate (this is a sublty but important to understand why this is built):
   - **The nested templates** (`modules/**`, `failover.yaml`) are fetched by the **CloudFormation service itself**, not by your instances. That fetch never touches your VPC, BIG-IPs, or their EIPs, and CloudFormation requires the template's S3 bucket to be in the **same Region/partition as the stack** — it cannot use F5's commercial `f5-cft-v2` bucket. So the templates **must** be staged in a GovCloud bucket **regardless of any public IPs**. Public EIPs do *not* change this.
   - **The BIG-IP artifacts** (the runtime-init `.run` installer and the DO/AS3/CF RPMs) are downloaded **by each BIG-IP at boot**. If the BIG-IP has internet egress (e.g. a public management EIP, or a public external Self-IP with a route), it *can* reach `cdn.f5.com` / GitHub directly — so staging these is **required only for air-gap**, and optional when you have egress (you could instead point the artifact-URL parameters at the public sources). In practice, staging them in the same bucket is simplest: the solution's own `runtime-init-conf-*.yaml` files live there, and the template defaults already point every artifact URL at your bucket.

   Bottom line: **templates should always be staged** (a CloudFormation/partition constraint, independent of EIPs); **artifacts are staged for air-gap** or left on public sources when the BIG-IP has egress. Its best practice to just stage everything in a S3 bucket. Instructions are below to do just this.
2. **Air-gapped accounts have no internet egress at all.** The BIG-IPs cannot phone home, download anything, or reach the public AWS service endpoints. Everything they need must be reachable **inside the VPC** — which means staging artifacts in S3 and provisioning **VPC endpoints** so the BIG-IPs can reach S3, EC2, Secrets Manager, and CloudFormation privately (see [§9](#9-air-gap-choose-your-public-ip-toggles-and-endpoints)).

This guide walks through both: a standard GovCloud deployment, and the additional steps for a fully air-gapped one.

---

## 2. Prerequisites

Before you start, make sure you have:

- **An AWS GovCloud (US) account** and credentials, with permission to create CloudFormation stacks, VPCs, EC2 instances, IAM roles (`CAPABILITY_NAMED_IAM`), S3 buckets, Secrets Manager secrets, and (for air-gap) VPC endpoints. You will need programmatic access keys like: Access Key, Secret Access Key and Session Token
- **The AWS CLI** configured for your GovCloud account (`aws configure` with your GovCloud access keys), or Console access to the GovCloud CloudFormation service. These steps are outside the scope of this guide but you can easily Google this. Installing the AWS CLI is easy and will make your life easier when transferring the artifacts to your S3 bucket.
- **A clone of this repository** (you will run `aws s3 sync` from its root). After you clone this repo then CD to it and run the "awss s3 sync" commands from this root.
- **The three BIG-IP extension RPMs and the runtime-init installer** (see [Step 5](#5-stage-the-templates-and-bigip-artifacts-in-your-bucket)). These are **not** in the repo and must be downloaded once (from a machine with internet) and staged into your bucket. 
- **Enough Elastic IP (EIP) quota.** This is the single most common deployment blocker — see the warning below.
- **AWS Objects** You will need to create a S3 bucket for staging artifacts(this repo and RPM's), SSH Key Pair, and a Secret created in Secrets Manager. Create these in the region you will deploy in, this is called out below.

> ### ⚠️ Elastic IP quota — read this first
> The default AWS quota is **5 Elastic IPs per Region**, and is frequently **lower** in GovCloud / locked-down accounts. This solution can provision **up to 7 EIPs**. If you exceed the quota the deployment fails with `AddressLimitExceeded` (usually surfaced as a failure to create `BigipManagementEipAddress01`). You will see this in the CloudFormation Template GUI after you launch it or from the output from your CLI command.
>
> | Configuration | EIPs used |
> | --- | --- |
> | Default (public mgmt + public VIP + public Self IPs) | **7** |
> | Public mgmt + private VIP + public Self IPs | 6 |
> | **Air-gap: public mgmt only, private VIP, private Self IPs** | **4** |
> | Private mgmt (bastion) + private VIP + private Self IPs | 3 |
>
> EIP consumers: 2 × NAT gateway (1/AZ, always created), 2 × external Self IP (toggle), 2 × management (toggle), 1 × VIP (toggle).
>
> **Before deploying:** confirm your quota (Service Quotas → *EC2-VPC Elastic IPs*, code `L-0263D0A3`) and either request an increase or reduce EIP usage with the public-IP toggles in [Step 7](#7-air-gap-choose-your-public-ip-toggles-and-endpoints). Also note: a **failed stack that you leave standing still holds its EIPs** — delete it and release orphaned addresses before retrying (see [Troubleshooting](#elastic-ip-limit-exceeded)).

---

## 3. Decide your Region — and keep it consistent

Pick **one** GovCloud Region (e.g. `us-gov-east-1` or `us-gov-west-1`) and use it for **everything**:

- the **staging S3 bucket**
- the **CloudFormation stack** (this is what the Console Region selector / CLI `--region` controls)
- the **EC2 key pair**
- the **Secrets Manager secret**
- (air-gap) the **VPC endpoints**

> **Why this matters:** VPC endpoints and the S3 gateway endpoint are **regional** by design (`com.amazonaws.<region>.s3`). An endpoint in one Region cannot reach a bucket in another. A cross-region deployment may *appear* to work over public S3, but it **breaks air-gap** and is easy to get wrong. The Region you choose in the Console's top-right selector (or `--region`) decides where **all resources** deploy — this is independent of where the template *files* are stored, so it's easy to deploy into the wrong Region by accident. **Set them the same and double-check.**

Throughout this guide the examples use `us-gov-east-1` and a bucket named `f5-cft-gov`. Substitute your own. If you are in the official PubSec Gov Cloud tenant you might see this bucket, if its there then you can use the artifacts in the bucket. Just create your key pair and secret and then reference them in template.

```bash
REGION=us-gov-east-1
BUCKET=f5-cft-gov                                  # must be globally unique
PREFIX=f5-aws-cloudformation-v2/v3.6.0.0/examples
```

---

## 4. Create the admin password secret

The BIG-IP admin password is stored in **AWS Secrets Manager**, and **both BIG-IPs read the same secret** so they share an identical admin password (this is required for clustering — see [Troubleshooting](#clustering-does-not-form--no-trust-domain)).
This can also be created via the AWS GUI. Those steps are outside the scope of this guide but a companion DevCentral article will be written and will include screenshots of this process. For this guide use the AWS CLI commands or simple Google-fu for the GUI instructions.

```bash
aws secretsmanager create-secret \
  --region "$REGION" \
  --name f5-bigip-admin \
  --secret-string 'YOUR-STRONG-PASSWORD' \
  --query ARN --output text
```

Record the returned ARN — it must be in your partition (`arn:aws-us-gov:secretsmanager:...`). You will pass it as **`bigIpSecretArn`** at launch in the template.

> **Rule: pick a stable password and do not edit/rotate the secret around a deployment.** If the two instances ever resolve different password values, clustering silently fails ("no trust domain"). After deploy you can verify both nodes accept it:
> `curl -sku admin:'YOUR-PASSWORD' https://<each-mgmt-ip>/mgmt/tm/sys/version` → both should return `200`.

---

## 5. Create an SSH key pair

You log in to the BIG-IP **management shell** with an SSH key (the GUI/API use the Secrets Manager password). **Pre-create the key pair** rather than letting the stack auto-generate one — it's much easier to retrieve.

```bash
aws ec2 create-key-pair --region "$REGION" --key-name f5-bigip-key \
  --query KeyMaterial --output text > f5-bigip-key.pem
chmod 400 f5-bigip-key.pem
```

You will pass `sshKey=f5-bigip-key` at launch.

> The key pair is **regional** — it must exist in the same Region as the stack, or launch fails with `InvalidKeyPair.NotFound`. If you instead leave `sshKey` blank, the stack creates `<uniqueString>-keyPair` and the private key is only retrievable from **SSM Parameter Store** at `/ec2/keypair/<KeyPairId>` (`aws ssm get-parameter --with-decryption`) — easy to miss. Pre-creating is simpler.

Later, you connect with:
```bash
ssh -i f5-bigip-key.pem admin@<bigip-mgmt-ip>
```
Or if DO has completed, then you should be able to use your Secret that you created in SecretsManager. ssh admin@<bigip-mgmt-ip>
---

## 6. Find the BIG-IP image in your Region

The template looks up the BIG-IP AMI by **name pattern** (`bigIpImage`). Marketplace image availability differs between Regions, so confirm the image exists in **your** Region first:

```bash
aws ec2 describe-images --region "$REGION" --owners aws-marketplace \
  --filters "Name=name,Values=*17.5*PAYG-Best Plus 25Mbps*" \
  --query 'reverse(sort_by(Images,&CreationDate))[].[Name,ImageId,CreationDate]' --output table
```

Pick the build you want (the newest is usually best), and set `bigIpImage` to a pattern pinned to that exact version/build, wildcarding the trailing timestamp. For example, for `F5 BIGIP-17.5.1.6-0.0.25 PAYG-Best Plus 25Mbps-260423140103`:

```
*17.5.1.6-0.0.25*PAYG-Best Plus 25Mbps*
```

> The template default is already set to a known-good GovCloud 17.5.x build. If your Region offers a newer one, update the `bigIpImage` parameter (or the template default) accordingly. If the lookup finds nothing, the deployment fails early at **`AmiInfo`** with *"AMIs … have not been found"* — that just means the pattern matched no image in that Region/partition.

---

## 7. Stage the templates and BIG-IP artifacts in your bucket

The BIG-IPs and CloudFormation both read everything from your bucket. There are two kinds of objects:

- **Nested templates** (`modules/**`, `failover/**`) — copied by `aws s3 sync`.
- **BIG-IP artifacts** — the runtime-init installer (`.run`) and the three extension RPMs. These are **not in the repo**, so `s3 sync` will not copy them; you must `aws s3 cp` them separately.

### 7a. Download the BIG-IP artifacts (once, from a machine with internet)

These four files are published by F5 on **GitHub Releases**. Download them on an internet-connected machine — they are **not** reachable from inside an air-gapped GovCloud VPC, which is exactly why you stage them in your bucket. The versions below match the hashes pinned in the runtime-init config files; if you change a version, also update its `extensionVersion`/`extensionHash` in the `runtime-init-conf-*.yaml` files.

```bash
# F5 BIG-IP Runtime Init installer  (note: this repo's tag has NO "v" prefix)
curl -fL -o f5-bigip-runtime-init-2.0.3-1.gz.run \
  https://github.com/F5Networks/f5-bigip-runtime-init/releases/download/2.0.3/f5-bigip-runtime-init-2.0.3-1.gz.run

# Declarative Onboarding (DO)
curl -fL -o f5-declarative-onboarding-1.47.0-14.noarch.rpm \
  https://github.com/F5Networks/f5-declarative-onboarding/releases/download/v1.47.0/f5-declarative-onboarding-1.47.0-14.noarch.rpm

# Application Services (AS3)  
curl -fL -o f5-appsvcs-3.56.0-10.noarch.rpm \
  https://github.com/F5Networks/f5-appsvcs-extension/releases/download/v3.56.0/f5-appsvcs-3.56.0-10.noarch.rpm

# Cloud Failover Extension (CFE)
curl -fL -o f5-cloud-failover-2.4.0-0.noarch.rpm \
  https://github.com/F5Networks/f5-cloud-failover-extension/releases/download/v2.4.0/f5-cloud-failover-2.4.0-0.noarch.rpm
```

> If a URL returns 404, the version/tag was renamed — open the repo's **Releases** page and copy the current asset link (the extension tags use a `v` prefix, e.g. `v1.47.0`; the runtime-init tag does not, e.g. `2.0.3`).

**Verify integrity (recommended for air-gap).** Each release also publishes a matching `.sha256`, but the simplest check is to confirm the three RPM sums equal the `extensionHash` values already pinned in the runtime-init config — the BIG-IP enforces those at install time:

```bash
sha256sum f5-declarative-onboarding-1.47.0-14.noarch.rpm \
          f5-appsvcs-3.56.0-10.noarch.rpm \
          f5-cloud-failover-2.4.0-0.noarch.rpm
# Compare against the extensionHash fields in:
#   examples/failover/bigip-configurations/runtime-init-conf-3nic-payg-instance01-with-app.yaml
```

Bucket destinations (used in Step 7b): the `.run` installer goes to `${PREFIX}/`, and the three RPMs go to `${PREFIX}/bigip-extensions/`.

### 7b. Create the bucket and upload everything

```bash
# Create the bucket
aws s3api create-bucket --bucket "$BUCKET" --region "$REGION" \
  --create-bucket-configuration LocationConstraint="$REGION"

# Sync the templates (run from the ROOT of this repository)
aws s3 sync ./examples/ "s3://$BUCKET/$PREFIX/" --region "$REGION"

# Copy the artifacts that s3 sync does NOT include
aws s3 cp f5-bigip-runtime-init-2.0.3-1.gz.run        "s3://$BUCKET/$PREFIX/" --region "$REGION"
aws s3 cp f5-declarative-onboarding-1.47.0-14.noarch.rpm "s3://$BUCKET/$PREFIX/bigip-extensions/" --region "$REGION"
aws s3 cp f5-appsvcs-3.56.0-10.noarch.rpm                "s3://$BUCKET/$PREFIX/bigip-extensions/" --region "$REGION"
aws s3 cp f5-cloud-failover-2.4.0-0.noarch.rpm          "s3://$BUCKET/$PREFIX/bigip-extensions/" --region "$REGION"
```

> If you later edit any template or runtime-init config locally, **re-`cp` that file to the bucket** before redeploying — the BIG-IPs read the bucket copy, not your local one.

---

## 8. Make the artifacts readable by the BIG-IP (REQUIRED)

This step is **required**, not optional — the BIG-IP cannot onboard without it, and skipping it is the classic "everything deployed but nothing got configured and then it rolled back" failure.

**Why:** CloudFormation fetches the nested *templates* with **your IAM credentials** (a private bucket is fine for those). But each **BIG-IP downloads the `.run` installer, the `runtime-init-conf-*.yaml` files, and the RPMs at boot via unauthenticated HTTPS `curl`** — no AWS signature. If those objects are not anonymously readable, the BIG-IP gets **HTTP 403**, runtime-init never installs, no DO/AS3/CFE is applied, and the stack rolls back and deletes the instances — often with no obvious CloudFormation error.

GovCloud enables S3 Block Public Access and disables ACLs by default, so you grant read access with a **bucket policy** (not ACLs). Clearing Block Public Access alone grants nothing — you must also apply the policy.

```bash
# 1) Allow a public bucket policy (ACL blocking can stay on)
aws s3api put-public-access-block --bucket "$BUCKET" \
  --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=false,RestrictPublicBuckets=false
```

```json
// bucket-policy.json  — replace the bucket name (note: arn:aws-us-gov: partition)
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "PublicReadGetObject",
      "Effect": "Allow",
      "Principal": "*",
      "Action": "s3:GetObject",
      "Resource": "arn:aws-us-gov:s3:::f5-cft-gov/*"
    },
    {
      "Sid": "DenyInsecureTransport",
      "Effect": "Deny",
      "Principal": "*",
      "Action": "s3:*",
      "Resource": [
        "arn:aws-us-gov:s3:::f5-cft-gov",
        "arn:aws-us-gov:s3:::f5-cft-gov/*"
      ],
      "Condition": { "Bool": { "aws:SecureTransport": "false" } }
    }
  ]
}
```

```bash
aws s3api put-bucket-policy --bucket "$BUCKET" --policy file://bucket-policy.json
```

**Security notes:** grant `s3:GetObject` only — never `PutObject`/`DeleteObject` to `Principal: "*"`. `s3:ListBucket` is intentionally omitted (the bucket can't be enumerated). If you know your consumer AWS account IDs, prefer scoping `Principal` to those accounts over `"*"`.

**Verify** anonymous read of the actual BIG-IP artifacts — this is exactly what the BIG-IP does at boot. All must return **HTTP 200**:

```bash
for KEY in \
  "${PREFIX}/failover/failover.yaml" \
  "${PREFIX}/f5-bigip-runtime-init-2.0.3-1.gz.run" \
  "${PREFIX}/failover/bigip-configurations/runtime-init-conf-3nic-payg-instance01-with-app.yaml" \
  "${PREFIX}/bigip-extensions/f5-declarative-onboarding-1.47.0-14.noarch.rpm"; do
  printf '%s  %s\n' "$(curl -sk -o /dev/null -w '%{http_code}' "https://${BUCKET}.s3.${REGION}.amazonaws.com/${KEY}")" "$KEY"
done
```

> **403** = policy/BPA not effective yet. **404** on the `.run` or an `.rpm` = the file was never uploaded (it isn't part of `s3 sync` — `aws s3 cp` it as in Step 7b).

### Air-gapped / cannot-make-public alternatives

If your security posture forbids a public bucket:

- **An S3 Gateway endpoint alone does NOT fix the 403** — the default bootstrap sends an **unsigned** request, which a private bucket rejects regardless of the endpoint. The endpoint only helps once the request is **IAM-signed**.
- **Alternative A — IAM-signed pulls (keeps the bucket private):** scope the bucket policy to the BIG-IP instance role, leave BPA on, deploy the S3 Gateway endpoint, and have the bootstrap fetch artifacts with signed requests. Fully in-partition, no public exposure. Requires customizing how runtime-init artifacts are fetched.
- **Alternative B — pre-signed URLs:** pass pre-signed S3 URLs via `bigIpRuntimeInitPackageUrl`, `bigIpRuntimeInitConfig01/02`, and the RPM `extensionUrl`s.
- **Alternative C — custom AMI:** bake the artifacts into a custom BIG-IP image with the [F5 Image Generation Tool](https://clouddocs.f5.com/cloud/public/v1/ve-image-gen_index.html).

Public-read is simplest; the above are the air-gap-clean options.

---

## 9. (Air-gap) Choose your public-IP toggles and endpoints

If your environment has internet egress (NAT/IGW), you can skip most of this and accept the defaults. For **air-gap**, set these:

| Parameter | Air-gap value | Effect |
| --- | --- | --- |
| `provisionPublicIpMgmt` | `true` (or `false` + bastion) | Public EIP on the BIG-IP management interface so you can reach the GUI/SSH. Set `false` to use a bastion only. |
| `provisionPublicIpExternalSelf` | `false` | No EIP on the external Self IPs (saves 2 EIPs). |
| `provisionExternalVip` | `true` | A floating application VIP exists. |
| `provisionPublicIpVip` | `false` | The VIP is **private** (no public EIP) — air-gap. |
| **`provisionS3Endpoint`** | **`true`** | **Required for air-gap.** Provisions the S3 Gateway endpoint **and** the EC2, Secrets Manager, and CloudFormation **interface** endpoints the BIG-IPs need privately. |
| `provisionExampleApp` | `false` (default) | The demo app pool member needs Docker Hub egress; off by default. |
| `allowUsageAnalytics` | `false` (default) | No phone-home telemetry. |

> ### Why `provisionS3Endpoint=true` is mandatory for air-gap
> Once you turn **off** the external Self IP EIPs, the BIG-IP dataplane has no internet path. Four things then break without VPC endpoints:
> - **AS3** can't fetch the WAF policy from S3 (`ECONNREFUSED`) — and AS3 has no retry, so this is a terminal failure → rollback.
> - **CFE** can't reach the **EC2** API (to move NICs/addresses/routes) or **Secrets Manager** → `Failover initialization failed: undefined undefined`.
> - The **clustering self-heal** can't reach **CloudFormation** to send its stack-success signal (see [§12](#12-automatic-clustering-recovery-the-self-heal)) → the stack would time out to CREATE_FAILED even though the cluster formed.
>
> Setting `provisionS3Endpoint=true` provisions **all four** endpoints (S3 gateway + EC2, Secrets Manager, and CloudFormation interface, with private DNS) in one toggle. The S3 gateway endpoint is free; the interface endpoints incur hourly + data charges. The bucket, the deployment, and the endpoints must all be in the **same Region**.

This **4-EIP air-gap profile** (public mgmt, private VIP, private Self IPs) fits the default quota of 5.

---

## 10. Launch the stack

> ### Rollback: optional (it was required during bring-up; the stack now self-completes)
> The stack **completes on its own** — the self-heal ([§12](#12-automatic-clustering-recovery-the-self-heal)) forms the cluster and sends the CloudFormation success signal — so **normal rollback-on-failure is fine for production** (a genuine failure cleans up as usual). For a **first deploy in a new environment**, you may still prefer rollback **disabled** (CLI `--disable-rollback`, or Console **Stack failure options → Preserve successfully provisioned resources**) as a safety net: if something environment-specific goes wrong during the ~25-30 min the self-heal runs, the instances survive so you can read `/config/cluster-heal/log` instead of losing them. *(If you disable rollback and a deploy fails, delete the stack and release orphaned EIPs before retrying.)*

### Option A — AWS Console (GUI)

The public "Launch Stack" button points at F5's commercial bucket and **will not work in GovCloud**. Instead:

1. In the GovCloud Console (correct Region selected), go to **CloudFormation → Create stack → With new resources (standard)**.
2. **Prerequisite – Prepare template:** *Choose an existing template*.
3. **Specify template:** *Amazon S3 URL*, and paste your `failover.yaml` URL:
   ```
   https://<s3BucketName>.s3.<s3BucketRegion>.amazonaws.com/<artifactLocation>failover/failover.yaml
   # e.g. https://f5-cft-gov.s3.us-gov-east-1.amazonaws.com/f5-aws-cloudformation-v2/v3.6.0.0/examples/failover/failover.yaml
   ```
4. **Next**, then set parameters. **Confirm `s3BucketName` / `s3BucketRegion` / `artifactLocation` match the bucket in your URL** so the nested stacks and BIG-IP artifacts are pulled from *your* bucket. Set `sshKey`, `bigIpSecretArn`, `restrictedSrcAddressMgmt`, `restrictedSrcAddressApp`, and the air-gap toggles from Steps 4–9.
5. **Review:** acknowledge **`CAPABILITY_NAMED_IAM`** (optionally set **Stack failure options → Preserve successfully provisioned resources** for a first-run safety net — see the rollback note above), then **Create stack**.

### Option B — AWS CLI

```bash
aws cloudformation create-stack --region "$REGION" --stack-name myFailover \
  --template-url "https://${BUCKET}.s3.${REGION}.amazonaws.com/${PREFIX}/failover/failover.yaml" \
  --parameters \
    "ParameterKey=s3BucketName,ParameterValue=${BUCKET}" \
    "ParameterKey=s3BucketRegion,ParameterValue=${REGION}" \
    "ParameterKey=artifactLocation,ParameterValue=${PREFIX}/" \
    "ParameterKey=sshKey,ParameterValue=f5-bigip-key" \
    "ParameterKey=bigIpSecretArn,ParameterValue=arn:aws-us-gov:secretsmanager:${REGION}:ACCOUNT_ID:secret:f5-bigip-admin-XXXXXX" \
    "ParameterKey=restrictedSrcAddressMgmt,ParameterValue=YOUR.ADMIN.CIDR/32" \
    "ParameterKey=restrictedSrcAddressApp,ParameterValue=10.0.0.0/8" \
    "ParameterKey=provisionPublicIpExternalSelf,ParameterValue=false" \
    "ParameterKey=provisionPublicIpVip,ParameterValue=false" \
    "ParameterKey=provisionS3Endpoint,ParameterValue=true" \
  --capabilities CAPABILITY_NAMED_IAM
```
> For a first deploy in a new environment you can append `--disable-rollback` as a safety net (see the rollback note above); for production it's optional.

> `artifactLocation` **must end in `/`**. Omit the air-gap toggles for a standard (egress-available) deployment.

---

## 11. Validate the deployment

### 11a. Stack + BIG-IP onboarding

1. Wait for the stack to reach **CREATE_COMPLETE**. If a nested stack fails, open it → **Events** → read the **Status reason**.
2. Get the management IP from the stack **Outputs** (`bigIpManagementPublicIp01` if `provisionPublicIpMgmt=true`, otherwise via the bastion outputs).
3. SSH in and confirm onboarding finished:
   ```bash
   ssh -i f5-bigip-key.pem admin@<bigip-mgmt-ip>
   grep -i 'All operations completed successfully' /var/log/cloud/bigIpRuntimeInit.log
   ```
   The prompt should show the hostname (`failover01.local` / `failover02.local`), not `ip-x-x-x-x`.

### 11b. Clustering

On **either** box:
```bash
tmsh show cm sync-status
```
Expect **`Status: In Sync`** (green), **`Mode: high-availability`**, all device groups in sync. One box reports `Active`, the other `Standby` for the traffic group.

> If sync-status shows **"no trust domain" / standalone** and never clusters, that is the documented device-trust startup-timing issue — see the recovery in [Troubleshooting](#clustering-does-not-form--no-trust-domain).

### 11c. CFE

```bash
curl -sku admin:'YOUR-PASSWORD' https://localhost/mgmt/shared/cloud-failover/inspect | python3 -m json.tool | head -40
```
A populated response (instance, addresses, trafficGroup) means CFE can move the floating addresses/routes on failover.

---

## 12. Automatic clustering recovery (the self-heal)

### Why this exists
There is a **documented BIG-IP / Declarative Onboarding platform bug** in the 17.x line: *"the trust domain (`/Common/Root`) is not fully initialised after system startup."* When it hits, DO cannot create the device-trust or the failover device-group during onboarding, the cluster never forms, and `/var/log/restnoded/restnoded.log` shows:
- `01020036:3: The requested trust domain (/Common/Root) was not found.`
- `01020036:3: The requested device group (/Common/failoverGroup) was not found.`

F5's documented workaround is: **reboot** (which rebuilds `Root`), then **re-apply** clustering. F5's KB scopes this to 17.5.x, but this solution **reproduced the identical failure on 17.1.3.2** as well — so treat it as affecting the **17.x line generally (and possibly other versions)**. It's a platform-timing issue, **not** caused by GovCloud, the security groups, the endpoints, or this template — and you **cannot** dodge it by picking a different 17.x image (we tried).

Because the point of a template is to remove manual steps, this solution ships a **self-heal** that automates F5's documented workaround. Importantly, **the DO declaration is left stock** — DO remains the declarative source of truth for clustering; the self-heal only *bootstraps* what the platform bug prevented DO from completing, and the resulting cluster matches the DO declaration (no config drift, nothing for a future DO re-apply/upgrade to tear down). On a BIG-IP build where the bug doesn't occur, the self-heal simply sees the cluster already In Sync and disables itself — it is a **safety net, not a dependency**.

### What it is
The failover runtime-init config installs, via its `pre_onboard` hook, two small files plus a cron job:
- `/config/cluster-heal.sh` — the orchestrator
- `/config/cluster-heal-trust.py` — fetches the admin password from Secrets Manager (SigV4, using the instance IAM role) and calls `add-to-trust` (the BIG-IP has no `aws` CLI / `boto3`, so this is stdlib-only)
- `/etc/cron.d/cluster-heal` — runs the orchestrator every 3 minutes

Repo sources: `examples/failover/bigip-configurations/cluster-heal.sh` and `cluster-heal-trust.py` (base64-embedded into the runtime-init configs; regenerate with `base64 -w0`).

### The workflow (runs per box every 3 min, marker-gated so it never loops)
1. **Already In Sync?** → send the CloudFormation success signal, remove the cron, mark done, stop.
2. Wait until base onboarding has set the hostname and the box has been up long enough (pre-reboot guard, ~10 min).
3. **`Root` missing?** (the bug) → `tmsh save sys config` and **reboot once** (rebuilds `Root`).
4. After the reboot, **trust not formed?**
   - The **joiner** (second BIG-IP — the one whose rendered `trust.remoteHost` is an IP) reads the peer address/hostname from the runtime-init log, fetches the admin password from Secrets Manager, and runs `add-to-trust` against the peer.
   - The **owner** (first BIG-IP) just waits for the joiner.
5. **Trust formed?** → the elected owner creates the `failoverGroup` device-group, force-syncs, and each box handles any `Synchronize this device to group …` recommendation (covers `datasync-global-dg`).
6. **In Sync** → send the CloudFormation success signal (so the stack reaches CREATE_COMPLETE), then disable itself.

The CloudFormation signal in steps 1/6 is why **`provisionS3Endpoint=true` also provisions a CloudFormation VPC endpoint** (see [§9](#9-air-gap-choose-your-public-ip-toggles-and-endpoints)): with no dataplane egress the BIG-IP can't reach `cloudformation.<region>.amazonaws.com`, and the stack would time out even though the cluster formed.

### What to expect on a deploy
- The stack stays **`CREATE_IN_PROGRESS` for ~25-30 minutes** while the self-heal reboots and forms the cluster — this is normal — then flips to **`CREATE_COMPLETE`**. (The CreationPolicy timeout is `PT50M`.)
- No manual steps; the cluster comes up on its own and the stack signals success only once it is genuinely In Sync (a real failure still surfaces honestly as CREATE_FAILED via the timeout).

### Where the logs are / how to watch it
- **`/config/cluster-heal/log`** — the self-heal's own narration; this is the primary place to look. You'll see the pre-reboot waits → reboot → `add-to-trust` → `creating failoverGroup` → sync → `cluster In Sync` → `cfn-signal sent OK` → `disabling self-heal`.
- **`/config/cluster-heal/`** marker files: `rebooted`, `trust_tries`, `signalled`, `done`.
- `tmsh show cm sync-status` — cluster state (target: green / In Sync).
- `/var/log/cloud/bigIpRuntimeInit.log`, `/var/log/restnoded/restnoded.log`, `/var/log/ltm` — onboarding / DO / device-trust detail.

### Troubleshooting the self-heal
- **>30 min and the stack is still in progress** → `cat /config/cluster-heal/log` on both boxes; the last line names the phase. If you see **`cfn-signal failed`**, the BIG-IP can't reach CloudFormation — confirm `provisionS3Endpoint=true` and that `curl -sk --max-time 10 https://cloudformation.<region>.amazonaws.com/` returns a number (not `000`).
- **`add-to-trust … No route to host`** right after the reboot → harmless; the metadata service isn't up yet and the next 3-min tick retries automatically (it self-corrected in testing).
- **`Root STILL missing >4min after reboot - manual recovery needed`** or **`add-to-trust attempted 6x …`** → the self-heal exhausted its automatic attempts; fall back to the manual recovery in [Troubleshooting → Clustering does not form](#clustering-does-not-form--no-trust-domain).
- **Re-arm it** (e.g., after manual changes): `rm -f /config/cluster-heal/done /config/cluster-heal/signalled`, restore the cron line (`echo '*/3 * * * * root /config/cluster-heal.sh >/dev/null 2>&1' > /etc/cron.d/cluster-heal`), or just run `/config/cluster-heal.sh` by hand.

> **This is a workaround for an F5 platform bug, not a permanent fix.** The right long-term resolution is the platform defect behind the KB. If you have F5 internal/support access, pull the defect ID for the authoritative root cause + target-fix build (and note the 17.1.3.2 repro — the KB's "17.5.x-only" scope is narrower than observed). When a fixed build is used, the self-heal becomes an inert safety net.

---

## 13. Parameter reference

Defaults below reflect this solution as configured for GovCloud. Anything marked *"leave at default unless you know you need to change it"* is mechanical and rarely touched. Parameters are grouped by the Console parameter sections.

### Template / artifact location
| Parameter | What it does | When to change |
| --- | --- | --- |
| `s3BucketName` | The S3 bucket holding the templates + BIG-IP artifacts. | **Set to your bucket.** |
| `s3BucketRegion` | The Region of that bucket. | **Set to your Region** (same as the deployment). |
| `artifactLocation` | The key prefix inside the bucket (must end in `/`). | Match where you staged the files. |

### BIG-IP image & size
| Parameter | What it does | When to change |
| --- | --- | --- |
| `bigIpImage` | Name pattern used to find the BIG-IP marketplace AMI. | Set to a build that exists in your Region ([Step 6](#6-find-the-bigip-image-in-your-region)). |
| `bigIpInstanceType` | EC2 instance type for the BIG-IPs. | Larger for more throughput; ENA-capable types boot/initialize faster. |
| `bigIpCustomImageId` | Use a specific AMI ID instead of the lookup. | Only if you baked a custom image. |

### Access & secrets
| Parameter | What it does | When to change |
| --- | --- | --- |
| `sshKey` | EC2 key pair name for admin SSH. | **Set to your pre-created key** ([Step 5](#5-create-an-ssh-key-pair)). |
| `bigIpSecretArn` | Secrets Manager ARN holding the admin password (shared by both nodes). | **Set to your secret** ([Step 4](#4-create-the-admin-password-secret)). |
| `restrictedSrcAddressMgmt` | CIDR allowed to reach BIG-IP management (SSH/GUI). | **Set to your admin network** — do not leave wide open. |
| `restrictedSrcAddressApp` | CIDR allowed to reach the application VIP. | Set to your client network. |
| `restrictedSrcPort` | Management GUI port used for the peer-to-peer rule. | Leave at default (443). |

### Public IP / air-gap toggles
| Parameter | What it does | When to change |
| --- | --- | --- |
| `provisionPublicIpMgmt` | Public EIP on management. | `false` to use a bastion only. |
| `provisionExternalVip` | Whether a floating application VIP exists. | `false` for Self-IPs-only. |
| `provisionPublicIpVip` | Whether that VIP is public (EIP) or private. | `false` = private VIP (air-gap). |
| `provisionPublicIpExternalSelf` | EIP on the external Self IPs. | `false` for air-gap (saves 2 EIPs). |
| `provisionS3Endpoint` | Provisions S3 gateway + EC2 + Secrets Manager VPC endpoints. | **`true` for air-gap (required).** |

### Application / telemetry
| Parameter | What it does | When to change |
| --- | --- | --- |
| `provisionExampleApp` | Deploys the demo application (needs Docker Hub egress). | `true` only if you have egress / a mirror. Default `false`. |
| `allowUsageAnalytics` | Sends anonymous usage stats to F5 (needs egress). | Leave `false` for GovCloud/air-gap. |

### Advanced — BIG-IP config & networking *(leave at default unless you know you need to change it)*
| Parameter | What it does |
| --- | --- |
| `bigIpRuntimeInitConfig01` / `02` | URL of each BIG-IP's runtime-init config. **Leave blank** to auto-derive from `s3BucketName`/`s3BucketRegion`/`artifactLocation`. Set only to bring your own BIG-IP config. |
| `bigIpRuntimeInitPackageUrl` | URL of the runtime-init installer. **Leave blank** to auto-derive. |
| `bigIpPeerAddr` | Address the second BIG-IP uses to reach the first for clustering (the first instance's management IP). | Leave at default unless you change the IP scheme. |
| `bigIpMgmtAddress01/02`, `bigIpExternalSelfIp01/02`, `bigIpInternalSelfIp01/02`, `bigIpExternalVip01/02` | Static private IPs for each interface/VIP. | Leave at default unless you change the subnet layout. |
| `bigIpHostname01/02` | Device hostnames (`failover01.local` / `failover02.local`). | Leave at default. |
| `cfeS3Bucket` | CFE failover-state bucket. **Leave blank** — the stack auto-creates and tags it (`<uniqueString>-bigip-high-availability-solution`). **Do not pre-create it.** |
| `uniqueString` | Prefix for named resources (IAM roles, key pair, etc.). | Use a **fresh** value per deployment to avoid IAM name collisions. |
| `application`, `cost`, `environment`, `group`, `owner` | Resource tags. | Set per your tagging policy. |

---

## 14. Troubleshooting

### Deployment fails at `AmiInfo`
The `bigIpImage` pattern matched no marketplace image in your Region/partition. List what's available (`aws ec2 describe-images … "Name=name,Values=*17.5*PAYG-Best Plus 25Mbps*"`) and set `bigIpImage` to a build that exists. (Historic note: an earlier failure here was caused by the AMI-lookup Lambda depending on a private F5 Lambda layer; that is already fixed in this repo's `modules/function/function.yaml`.)

### Elastic IP limit exceeded
`Client.AddressLimitExceeded` (often as `BigipManagementEipAddress01`). Either request a quota increase (`L-0263D0A3`) or reduce EIP usage with the public-IP toggles ([Step 9](#9-air-gap-choose-your-public-ip-toggles-and-endpoints)). **Failed stacks hold their EIPs** — delete them and release orphans:
```bash
aws ec2 describe-addresses --region "$REGION" --query 'Addresses[?AssociationId==`null`].[AllocationId,PublicIp]' --output table
aws ec2 release-address --region "$REGION" --allocation-id <AllocationId>
```

### "Everything deployed but nothing got configured" / HTTP 403 at boot
The bucket artifacts aren't anonymously readable. Re-check [Step 8](#8-make-the-artifacts-readable-by-the-bigip-required): the curl verify loop must return **200** for the `.run`, the `runtime-init-conf-*.yaml`, and each RPM. A **404** means a file wasn't uploaded (`aws s3 cp` it).

### AS3 `ECONNREFUSED` / CFE `Failover initialization failed: undefined`
The dataplane has no path to AWS service endpoints (you turned off the external Self-IP EIPs). Set **`provisionS3Endpoint=true`** and redeploy — it provisions the S3 + EC2 + Secrets Manager endpoints CFE and AS3 need ([Step 9](#9-air-gap-choose-your-public-ip-toggles-and-endpoints)). (CFE on GovCloud also requires the `scopingName` storage discovery rather than tag-based discovery; this is already wired into the runtime-init configs via the `cfeStorageName` instance tag.)

### Clustering does not form / "no trust domain"
**Symptom:** all stacks deploy (or the BIG-IP stack times out) but the pair never clusters. `tmsh show cm sync-status` → `Status: Unknown`, `Summary: no trust domain`, `Mode: standalone`; `tmsh list cm trust-domain` is empty. In `/var/log/restnoded/restnoded.log`:
- `01020036:3: The requested trust domain (/Common/Root) was not found.`
- `01020036:3: The requested device group (/Common/failoverGroup) was not found.`

**Cause:** a **documented BIG-IP / Declarative Onboarding behavior** — in some cases the local device-trust domain (`/Common/Root`) is **not fully initialized after system startup**, so DO cannot create the trust domain or the failover device group and clustering deadlocks (one device waits for `Root`, the other waits for the device group). See F5's DO troubleshooting: *"Why does BIG-IP cluster fail to form when using Declarative Onboarding…"* — https://clouddocs.f5.com/products/extensions/f5-declarative-onboarding/latest/troubleshooting.html . **This is a platform startup-timing condition — not caused by GovCloud, the security groups, the VPC endpoints, or this template.** Device trust and config-sync run over the **external Self IP** network; the management interface is not used for clustering.

> **This is normally handled automatically** by the clustering self-heal ([§12](#12-automatic-clustering-recovery-the-self-heal)) — it reboots to rebuild `Root`, re-establishes trust, builds the device-group, and signals the stack, with no manual steps. The steps below are the **manual fallback** for the rare case where the self-heal exhausts its automatic attempts (you'll see `manual recovery needed` in `/config/cluster-heal/log`). F5's documented workaround is the same sequence: *reboot → trust rebuilds automatically → reapply*.

**Recovery (validated):** Replace `<admin-password>` with your Secrets Manager value, `<bigip01-external-self-ip>` with BIG-IP-01's external Self IP (default `10.0.0.11`), and hostnames with yours.

1. **Reboot BOTH instances.** A clean boot lets `devmgmtd` rebuild the default `Root` trust domain; runtime-init is one-shot and won't re-run its failed clustering:
   ```bash
   tmsh reboot
   ```
2. After both are back, confirm `Root` now exists on each (lists the local device):
   ```bash
   tmsh list cm trust-domain one-line
   ```
3. On **BIG-IP-02**, add BIG-IP-01 to the trust domain over its **external Self IP** (not the management IP):
   ```bash
   tmsh modify cm trust-domain Root ca-devices add { <bigip01-external-self-ip> } name failover01.local username admin password <admin-password>
   ```
   `tmsh list cm trust-domain one-line` should now show **both** devices, status `initialized`.
4. Create the failover device group and synchronize (run on **BIG-IP-01**):
   ```bash
   tmsh create cm device-group failoverGroup type sync-failover
   tmsh modify cm device-group failoverGroup devices add { failover01.local failover02.local }
   tmsh modify cm device-group failoverGroup auto-sync enabled network-failover enabled
   tmsh save sys config
   tmsh run cm config-sync to-group failoverGroup
   ```
   If it stays `Changes Pending` / `Awaiting Initial Sync`, force the initial push from the device with the authoritative config:
   ```bash
   tmsh run cm config-sync force-full-load-push to-group failoverGroup
   ```
5. Verify on **both** boxes — expect `Status: In Sync` (green), `Mode: high-availability`:
   ```bash
   tmsh show cm sync-status
   ```

### Wrong Region / cross-region surprises
If air-gap downloads fail even though public S3 works, check you didn't deploy into a different Region than your bucket/endpoints. The S3 gateway endpoint is regional; everything must be in one Region ([Step 3](#3-decide-your-region--and-keep-it-consistent)).

### Clustering fails because the two nodes have different admin passwords
If trust never forms and `curl -sku admin:'PW' https://<peer-mgmt>/mgmt/tm/sys/version` returns `200` on one node and `401` on the other, the secret was edited so the instances resolved different values. Use a **stable** secret and redeploy ([Step 4](#4-create-the-admin-password-secret)).

### General log locations
- `/var/log/cloud/startup-script.log` — pre-runtime-init (artifact downloads).
- `/var/log/cloud/bigIpRuntimeInit.log` — onboarding; look for `All operations completed successfully`.
- `/var/log/restnoded/restnoded.log` — DO/AS3/CFE detail.
- `/var/log/ltm` — `mcpd`/`devmgmtd` (device trust / clustering).

---

## 15. Tear down

```bash
aws cloudformation delete-stack --region "$REGION" --stack-name myFailover
```

After deletion, confirm no orphaned EIPs remain (see [Elastic IP limit](#elastic-ip-limit-exceeded)). The CFE state bucket (`<uniqueString>-bigip-high-availability-solution`) and any auto-created key pair are removed with the stack; a bucket *you* created for staging is not (delete it separately if you no longer need it).

---

## Appendix — quick reference

| Thing | Value used in this guide |
| --- | --- |
| Region | `us-gov-east-1` |
| Staging bucket | `f5-cft-gov` |
| Artifact prefix | `f5-aws-cloudformation-v2/v3.6.0.0/examples/` |
| BIG-IP image | `*17.5.1.6-0.0.25*PAYG-Best Plus 25Mbps*` |
| Extensions | DO 1.47.0 · AS3 3.56.0 · CFE 2.4.0 |
| Runtime-init | 2.0.3 |
| Air-gap EIP profile | public mgmt + private VIP + private Self IPs = **4 EIPs** |
| Air-gap required toggle | `provisionS3Endpoint=true` |
| Rollback | optional — the stack self-completes; disable only as a first-run safety net |
</content>
</invoke>
