import json
import urllib.parse


def handler(event, context):
    del context

    processed_records = []

    for sqs_record in event.get("Records", []):
        body = sqs_record.get("body", "{}")
        message = json.loads(body)

        for s3_record in message.get("Records", []):
            bucket_name = s3_record["s3"]["bucket"]["name"]
            object_key = urllib.parse.unquote_plus(s3_record["s3"]["object"]["key"])

            print(f"Received object create event for s3://{bucket_name}/{object_key}")
            processed_records.append(
                {
                    "bucket": bucket_name,
                    "key": object_key,
                }
            )

    return {
        "statusCode": 200,
        "processed": processed_records,
    }
