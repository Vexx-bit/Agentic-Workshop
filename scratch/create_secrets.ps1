$project = "agentic-ai-workshop-506310"
$secrets = @("GOOGLE_API_KEY", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN")
foreach ($s in $secrets) {
    gcloud secrets create $s --replication-policy=automatic --project=$project 2>$null
    $val = (Get-Content .env | Where-Object { $_ -match "^${s}=(.*)" }) -replace "^${s}=", ""
    $tempFile = "temp_secret.txt"
    [System.IO.File]::WriteAllText($tempFile, $val)
    gcloud secrets versions add $s --data-file=$tempFile --project=$project
    Remove-Item $tempFile
}
