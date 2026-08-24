param(
    [Parameter(Mandatory = $false)]
    [string]$Date = (Get-Date -Format 'yyyy-MM-dd')
)

$ErrorActionPreference = 'Stop'
$baseUrl = 'https://np-anotice-stock.eastmoney.com/api/security/ann'
$pageSize = 100
$pageIndex = 1
$all = [System.Collections.Generic.List[object]]::new()

do {
    $query = @{
        sr            = '-1'
        page_size     = $pageSize
        page_index    = $pageIndex
        ann_type      = 'A'
        client_source = 'web'
        f_node        = '0'
        s_node        = '0'
        begin_time    = $Date
        end_time      = $Date
    }
    $queryString = ($query.GetEnumerator() | ForEach-Object {
        '{0}={1}' -f [uri]::EscapeDataString($_.Key), [uri]::EscapeDataString([string]$_.Value)
    }) -join '&'
    $response = Invoke-RestMethod -Uri "$baseUrl`?$queryString" -TimeoutSec 30
    if (-not $response.data) {
        throw "Announcement API returned no data for $Date"
    }
    foreach ($item in $response.data.list) {
        $all.Add($item)
    }
    $totalHits = [int]$response.data.total_hits
    $pageIndex++
} while ($all.Count -lt $totalHits)

function Get-ImpactClass([string]$Title) {
    if ($Title -match '立案|行政处罚|退市|暂停上市|终止上市|重大诉讼|重大仲裁|业绩预亏|业绩下降|业绩下滑|债务逾期|资金占用|违规担保|风险提示|严重异常波动') {
        return '风险/偏空'
    }
    if ($Title -match '业绩预增|业绩快报|重大合同|重大项目|中标|订单|增持计划|回购方案|回购报告书|首次回购|投资建设|扩建项目|收购.*股权|发行股份购买资产|重大资产重组|再融资注册|获得.*批复|获批上市|产品获批|利润分配方案') {
        return '潜在重要催化'
    }
    if ($Title -match '减持|减值|质押|冻结|终止|异常波动|问询函|监管函|担保|诉讼|仲裁') {
        return '需核验风险'
    }
    if ($Title -match '股东会|董事会|监事会|法律意见书|公司章程|管理制度|投资者关系|持股情况|月报表|权益分派实施|会议通知|独立董事|审计报告') {
        return '例行/程序性'
    }
    return '一般公告'
}

$rows = foreach ($item in $all) {
    $security = $item.codes | Select-Object -First 1
    $columns = ($item.columns | ForEach-Object { $_.column_name }) -join ';'
    $code = if ($security) { $security.stock_code } else { '' }
    [pscustomobject]@{
        notice_date  = $item.notice_date.Substring(0, 10)
        display_time = $item.display_time
        stock_code   = $code
        stock_name   = if ($security) { $security.short_name } else { '' }
        title        = $item.title
        impact_class = Get-ImpactClass $item.title
        categories   = $columns
        article_code = $item.art_code
        detail_url   = if ($code) { "https://data.eastmoney.com/notices/detail/$code/$($item.art_code).html" } else { '' }
    }
}

$outputDir = Join-Path $PSScriptRoot '..\announcements'
New-Item -ItemType Directory -Force -Path $outputDir | Out-Null
$csvPath = Join-Path $outputDir "${Date}_all_announcements.csv"
$publishedTodayCsvPath = Join-Path $outputDir "${Date}_published_today.csv"
$summaryPath = Join-Path $outputDir "${Date}_summary.txt"

$sorted = $rows | Sort-Object display_time -Descending
$sorted | Export-Csv -Path $csvPath -NoTypeInformation -Encoding utf8
$publishedToday = $sorted | Where-Object { $_.display_time -like "$Date*" }
$publishedToday | Export-Csv -Path $publishedTodayCsvPath -NoTypeInformation -Encoding utf8

$summary = @(
    "date=$Date"
    "total=$($sorted.Count)"
    "published_today=$($publishedToday.Count)"
    "companies=$(($sorted | Where-Object stock_code | Select-Object stock_code -Unique).Count)"
    "potential_catalysts=$(($sorted | Where-Object impact_class -eq '潜在重要催化').Count)"
    "risk_or_negative=$(($sorted | Where-Object impact_class -eq '风险/偏空').Count)"
    "risk_review=$(($sorted | Where-Object impact_class -eq '需核验风险').Count)"
    "routine=$(($sorted | Where-Object impact_class -eq '例行/程序性').Count)"
    "general=$(($sorted | Where-Object impact_class -eq '一般公告').Count)"
)
$summary | Set-Content -Path $summaryPath -Encoding utf8

$summary
"csv=$csvPath"
"published_today_csv=$publishedTodayCsvPath"
"summary=$summaryPath"

