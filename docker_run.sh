docker build -t donghua-downloader .

docker stop donghua-downloader
docker container rm donghua-downloader

docker run -d \
    -p 8765:8765 \
    --restart unless-stopped \
    --name donghua-downloader \
    --env-file .env \
    -e APP_DATA_PATH=/app/app_data.json \
    -v "$(pwd)/config.json:/app/config.json" \
    -v "$(pwd)/app_data.json:/app/app_data.json" \
    -v /volume1/Movies/Donghua:/downloads \
    -v /volume1/Movies/Others:/volume1/Movies/Others \
    donghua-downloader