# ec2_s3_monitor_combined.py
import boto3
from datetime import datetime, timedelta, timezone
import sys

# === CONFIG ===
INSTANCE_ID = "i-0xxxxxxxxxxxxxxxxx"   # <--- REPLACE
BUCKET_NAME = "my-bucket-name"         # <--- REPLACE
REGION = "ap-south-1"                  # <--- REPLACE
CPU_THRESHOLD = 10.0                   # percent
NET_THRESHOLD_MB = 10.0                # MB
LOOKBACK_DAYS_COST = 7
# =============

session = boto3.session.Session(region_name=REGION)
cloudwatch = session.client("cloudwatch")
ce = session.client("ce")  # cost explorer is a global endpoint but boto3 will handle it

def iso_now_utc():
    return datetime.now(timezone.utc)

def get_ec2_metrics(instance_id):
    end = iso_now_utc()
    start = end - timedelta(hours=1)

    queries = [
        {
            "Id": "cpu",
            "MetricStat": {
                "Metric": {"Namespace": "AWS/EC2", "MetricName": "CPUUtilization",
                          "Dimensions": [{"Name": "InstanceId", "Value": instance_id}]},
                "Period": 300,
                "Stat": "Average"
            },
            "ReturnData": True
        },
        {
            "Id": "net_in",
            "MetricStat": {
                "Metric": {"Namespace": "AWS/EC2", "MetricName": "NetworkIn",
                          "Dimensions": [{"Name": "InstanceId", "Value": instance_id}]},
                "Period": 300,
                "Stat": "Sum"
            },
            "ReturnData": True
        },
        {
            "Id": "net_out",
            "MetricStat": {
                "Metric": {"Namespace": "AWS/EC2", "MetricName": "NetworkOut",
                          "Dimensions": [{"Name": "InstanceId", "Value": instance_id}]},
                "Period": 300,
                "Stat": "Sum"
            },
            "ReturnData": True
        }
    ]

    resp = cloudwatch.get_metric_data(
        MetricDataQueries=queries,
        StartTime=start,
        EndTime=end,
        ScanBy='TimestampDescending'
    )

    def latest_value(results, default=0.0, stat='Average'):
        for r in results.get('MetricDataResults', []):
            if len(r.get('Values', [])) == 0:
                continue
            return r['Values'][0]
        return default

    cpu = latest_value(resp) or 0.0
    # locate net_in and net_out properly
    net_in_val = 0.0
    net_out_val = 0.0
    for r in resp.get('MetricDataResults', []):
        if r['Id'] == 'net_in' and r.get('Values'):
            net_in_val = r['Values'][0] / (1024.0 * 1024.0)  # bytes -> MB
        if r['Id'] == 'net_out' and r.get('Values'):
            net_out_val = r['Values'][0] / (1024.0 * 1024.0)

    return round(cpu, 2), round(net_in_val, 2), round(net_out_val, 2)

def get_s3_metrics(bucket):
    now = iso_now_utc()
    # Storage metrics are daily: use 2 day window to be safe
    start_daily = now - timedelta(days=2)
    # Request metrics: recent 1 hour with 60s or 300s depending on availability
    start_recent = now - timedelta(hours=1)

    queries = [
        {
            "Id": "bucket_size",
            "MetricStat": {
                "Metric": {"Namespace": "AWS/S3", "MetricName": "BucketSizeBytes",
                          "Dimensions": [
                                {"Name": "BucketName", "Value": bucket},
                                {"Name": "StorageType", "Value": "StandardStorage"}
                          ]},
                "Period": 86400,
                "Stat": "Average"
            },
            "ReturnData": True
        },
        {
            "Id": "obj_count",
            "MetricStat": {
                "Metric": {"Namespace": "AWS/S3", "MetricName": "NumberOfObjects",
                          "Dimensions": [
                                {"Name": "BucketName", "Value": bucket},
                                {"Name": "StorageType", "Value": "AllStorageTypes"}
                          ]},
                "Period": 86400,
                "Stat": "Average"
            },
            "ReturnData": True
        },
        # Request metric (requires S3 request metrics to be enabled; FilterId usually "EntireBucket")
        {
            "Id": "all_requests",
            "MetricStat": {
                "Metric": {"Namespace": "AWS/S3", "MetricName": "AllRequests",
                          "Dimensions": [
                                {"Name": "BucketName", "Value": bucket},
                                {"Name": "FilterId", "Value": "EntireBucket"}
                          ]},
                "Period": 300,
                "Stat": "Sum"
            },
            "ReturnData": True
        }
    ]

    # We'll do two get_metric_data calls so we can use different StartTime/Period semantics safely
    resp_daily = cloudwatch.get_metric_data(
        MetricDataQueries=[q for q in queries if q['Id'] in ('bucket_size','obj_count')],
        StartTime=start_daily,
        EndTime=now
    )
    resp_req = cloudwatch.get_metric_data(
        MetricDataQueries=[q for q in queries if q['Id'] == 'all_requests'],
        StartTime=start_recent,
        EndTime=now
    )

    # parse daily
    size_gb = 0.0
    obj_count = 0
    for r in resp_daily.get('MetricDataResults', []):
        if r['Id'] == 'bucket_size' and r.get('Values'):
            size_gb = r['Values'][0] / (1024.0 ** 3)
        if r['Id'] == 'obj_count' and r.get('Values'):
            obj_count = int(r['Values'][0])

    # parse requests
    requests = 0
    for r in resp_req.get('MetricDataResults', []):
        if r['Id'] == 'all_requests' and r.get('Values'):
            requests = int(r['Values'][0])

    return round(size_gb, 3), obj_count, requests

def get_ec2_cost(days=7):
    end = iso_now_utc().date()
    start = end - timedelta(days=days)
    resp = ce.get_cost_and_usage(
        TimePeriod={"Start": str(start), "End": str(end)},
        Granularity="DAILY",
        Metrics=["UnblendedCost"],
        Filter={
            "Dimensions": {
                "Key": "SERVICE",
                "Values": ["Amazon Elastic Compute Cloud - Compute"]
            }
        }
    )
    total = 0.0
    for day in resp.get('ResultsByTime', []):
        amt = day.get('Total', {}).get('UnblendedCost', {}).get('Amount')
        if amt:
            total += float(amt)
    return round(total, 4)

if __name__ == "__main__":
    try:
        print("\n--- EC2 + S3 Monitoring Report ---\n")
        cpu, net_in, net_out = get_ec2_metrics(INSTANCE_ID)
        size_gb, obj_count, s3_requests = get_s3_metrics(BUCKET_NAME)
        cost = get_ec2_cost(LOOKBACK_DAYS_COST)

        print(f"Instance: {INSTANCE_ID}")
        print(f"CPU (avg last 1 hr): {cpu}%")
        print(f"Network In (MB last 1 hr): {net_in}")
        print(f"Network Out (MB last 1 hr): {net_out}\n")

        print(f"Bucket: {BUCKET_NAME}")
        print(f"Bucket Size (GB): {size_gb}")
        print(f"Object Count (approx): {obj_count}")
        print(f"Total Requests (last 1 hr): {s3_requests}\n")

        print(f"EC2 Cost (last {LOOKBACK_DAYS_COST} days): ${cost}\n")

        if cpu < CPU_THRESHOLD and net_in < NET_THRESHOLD_MB and net_out < NET_THRESHOLD_MB:
            print("Status: EC2 appears underutilized — consider resizing or stopping.")
        else:
            print("Status: EC2 utilization looks normal.")

        if size_gb < 1 and obj_count < 50 and s3_requests < 20:
            print("Status: S3 bucket appears underutilized.")
        else:
            print("Status: S3 bucket usage looks normal.")
    except Exception as e:
        print("ERROR:", e)
        sys.exit(2)
