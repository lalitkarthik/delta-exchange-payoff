# R4 — the venue latency run, verbatim, and the run we cannot take yet

The measurement behind [0005-compute-and-region.md](0005-compute-and-region.md) §5, pasted
whole so it can be read without re-running it, and the exact commands for the from-AWS
measurement #68 asks for. The tool is `tools/measure_venue_latency.py`.

## 1. How it was taken

Run from `engine/` with the project venv, 2026-09-09:

```
.venv/Scripts/python.exe ../tools/measure_venue_latency.py \
    --label "this laptop (India), Cloudflare WARP on" --samples 20
```

Nothing subscribes. The websocket half opens a connection, completes the handshake, sends
twenty protocol-level pings and closes, so it cannot compete with the running engine for
Delta's frames. The REST half issues forty `GET /v2/products?page_size=1` calls — twenty
cacheable, twenty with a unique `_cb=` parameter that forces a CDN miss.

**The vantage point is the finding's own caveat.** This machine has **Cloudflare WARP**
active, so every packet is tunnelled and egresses at Cloudflare's `MAA` (Chennai) colo.
Absolute figures below are about this laptop, not about the network. The *difference*
between the cacheable and cache-busted rows survives, because both pay the same tunnel.

## 2. The run

```
# venue latency, vantage point: this laptop (India), Cloudflare WARP on
# 2026-09-09T13:36:00Z, 20 samples per figure

## 0. Vantage point
  egress seen by Cloudflare : ip=2a09:bac5:410f:11cd::1c6:8 colo=MAA loc=IN warp=on gateway=off
  *** WARP IS ON. Every figure includes a Cloudflare tunnel hop. ***

## 1. DNS and ownership   (ip-ranges.json createDate 2026-09-09-09-17-05)
  public-socket.india.delta.exchange
    CNAME chain : public-socket.india.delta.exchange -> d2gb279nx3imhv.cloudfront.net
    13.225.5.64                                AWS CLOUDFRONT in GLOBAL
    13.225.5.47                                AWS CLOUDFRONT in GLOBAL
    13.225.5.32                                AWS CLOUDFRONT in GLOBAL
    13.225.5.113                               AWS CLOUDFRONT in GLOBAL
    2600:9000:21b4:200:17:be:58c0:93a1         AWS CLOUDFRONT in GLOBAL
    2600:9000:21b4:f000:17:be:58c0:93a1        AWS CLOUDFRONT in GLOBAL
    2600:9000:21b4:fe00:17:be:58c0:93a1        AWS CLOUDFRONT in GLOBAL
    2600:9000:21b4:b000:17:be:58c0:93a1        AWS CLOUDFRONT in GLOBAL
    2600:9000:21b4:f400:17:be:58c0:93a1        AWS CLOUDFRONT in GLOBAL
    2600:9000:21b4:a400:17:be:58c0:93a1        AWS CLOUDFRONT in GLOBAL
    2600:9000:21b4:5200:17:be:58c0:93a1        AWS CLOUDFRONT in GLOBAL
    2600:9000:21b4:8000:17:be:58c0:93a1        AWS CLOUDFRONT in GLOBAL
  api.india.delta.exchange
    CNAME chain : api.india.delta.exchange -> d15zy4kc8a63om.cloudfront.net
    13.227.249.12                              AWS CLOUDFRONT in GLOBAL
    13.227.249.121                             AWS CLOUDFRONT in GLOBAL
    13.227.249.85                              AWS CLOUDFRONT in GLOBAL
    13.227.249.113                             AWS CLOUDFRONT in GLOBAL
    2600:9000:200a:ae00:1d:59e0:8980:93a1      AWS CLOUDFRONT in GLOBAL
    2600:9000:200a:c200:1d:59e0:8980:93a1      AWS CLOUDFRONT in GLOBAL
    2600:9000:200a:6e00:1d:59e0:8980:93a1      AWS CLOUDFRONT in GLOBAL
    2600:9000:200a:a00:1d:59e0:8980:93a1       AWS CLOUDFRONT in GLOBAL
    2600:9000:200a:b600:1d:59e0:8980:93a1      AWS CLOUDFRONT in GLOBAL
    2600:9000:200a:8c00:1d:59e0:8980:93a1      AWS CLOUDFRONT in GLOBAL
    2600:9000:200a:3200:1d:59e0:8980:93a1      AWS CLOUDFRONT in GLOBAL
    2600:9000:200a:fa00:1d:59e0:8980:93a1      AWS CLOUDFRONT in GLOBAL

## 2. TCP connect and TLS handshake
  public-socket.india.delta.exchange  (peer 13.225.5.64)
  TCP connect                n=20  min   99.07  p50  107.70  p95  128.95  max  128.95   ms
  TLS handshake              n=20  min  165.08  p50  173.23  p95  187.51  max  187.51   ms
  api.india.delta.exchange  (peer 13.227.249.12)
  TCP connect                n=20  min   98.85  p50  108.65  p95  139.34  max  139.34   ms
  TLS handshake              n=20  min   97.65  p50  170.30  p95  177.31  max  177.31   ms

## 3. REST round trip, edge cache hit and forced miss
  GET, cacheable             n=20  min  305.40  p50  398.43  p95  555.67  max  555.67   ms
  GET, cache-busted          n=20  min  521.00  p50  589.61  p95  844.74  max  844.74   ms
  cacheable                  Hit from cloudfront, POP BOM78-P11, origin nginx/1.18.0 (Ubuntu)
                             origin self-reported 34.17 ms inside itself
  cache-busted               Miss from cloudfront, POP BOM78-P11, origin nginx/1.18.0 (Ubuntu)
                             origin self-reported 58.07 ms inside itself
  miss minus hit              191.18 ms   -> edge<->origin ~95.6 ms one way

## 4. Websocket handshake and protocol ping  (no subscription sent)
  handshake (connect+TLS+HTTP upgrade) 1178.21 ms
  ping -> pong               n=20  min  236.49  p50  238.71  p95  243.58  max  243.58   ms

# Every figure above is `measured` on this machine, this run. A figure from a
# different vantage point is a different number; run this there with --label.
```

A repeat 14 minutes earlier gave TCP connect p50 111.84 ms, ping → pong p50 234.74 ms and
`miss minus hit` 184.87 ms — the same shape, run to run.

**`tracert -d -h 15` to both hosts**, same session: hop 1 `2a09:bac5::` (Cloudflare, the WARP
tunnel), hop 2 `2400:cb00:454:1000::1` (Cloudflare), then a Jio hop and seven more before
the CloudFront address answers at 96–104 ms. The tunnel is hop one, which is why nothing
here is a statement about an AWS instance's path to Delta.

## 3. The run we cannot take yet, exactly

`measured` from *two AWS regions* is an acceptance criterion of #68 and needs an AWS account,
which this machine does not have. When access arrives, this is the whole job — `assumed`
half an hour and two `t4g.micro` hours, under $0.02 of instance time:

```bash
# One throwaway instance per region. Amazon Linux 2023, arm64, public subnet, public IP.
for REGION in ap-south-1 ap-northeast-1; do
  AMI=$(aws ssm get-parameter --region "$REGION" \
        --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64 \
        --query Parameter.Value --output text)
  aws ec2 run-instances --region "$REGION" --image-id "$AMI" \
      --instance-type t4g.micro --associate-public-ip-address \
      --key-name "$KEY" --security-group-ids "$SG_SSH_OUT" \
      --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=r4-latency}]'
done

# On each instance, then paste both outputs back into this file with their --label:
sudo dnf install -y python3-pip git
pip3 install --user websockets
git clone https://github.com/lalitkarthik/delta-exchange-payoff.git
python3 delta-exchange-payoff/tools/measure_venue_latency.py \
    --label "$(curl -s http://169.254.169.254/latest/meta-data/placement/region)" \
    --samples 50

# And terminate. The instances exist only for the measurement.
aws ec2 terminate-instances --region "$REGION" --instance-ids "$ID"
```

**Add one third vantage point while the account is open**: the same tool from an
`ap-southeast-1` instance, because Singapore is the region #68 named and the only way to
know what its extra leg costs is to measure it.

**What the numbers will settle.** Three things, none of which this laptop can settle.

1. **Whether the CloudFront edge is the whole story.** If ap-south-1 and ap-northeast-1
   return the *same* websocket ping, the edge-to-origin leg is CloudFront's and identical
   from anywhere, and criterion 5 stops arguing for Tokyo. If Tokyo is materially faster,
   the CDN is passing the connection through and the region does buy freshness.
2. **What a region actually costs in milliseconds**, as a number to put beside the `derived`
   $137.75/month that ap-northeast-1 costs over ap-south-1 at ten times the rate.
3. **The number R5 (#69) is waiting for** — the same instance, against an ElastiCache
   endpoint in the same AZ and in a second AZ, per that ticket's plan. One trip, both
   answers.
