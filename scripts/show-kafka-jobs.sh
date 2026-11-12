#!/usr/bin/env sh
# Print every job message on the network-config-jobs topic (evidence screenshot for "Kafka job is visible").
docker compose exec kafka /opt/kafka/bin/kafka-console-consumer.sh \
  --bootstrap-server localhost:9092 --topic network-config-jobs --from-beginning \
  --property print.key=true --property print.partition=true --property print.offset=true \
  --timeout-ms 5000
echo
echo "Consumer group lag:"
docker compose exec kafka /opt/kafka/bin/kafka-consumer-groups.sh \
  --bootstrap-server localhost:9092 --describe --group netauto-workers
