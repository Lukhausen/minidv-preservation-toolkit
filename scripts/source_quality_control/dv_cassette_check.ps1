#!/usr/bin/env pwsh
# ============================================================================
# DV Cassette Archival Quality Checker
# ============================================================================
#
# Validates a folder of captured DV AVI files against archival preservation
# standards. Each folder represents one digitized MiniDV/DV cassette.
#
# Usage:
#   .\dv_cassette_check.ps1 <folder_path>
#   .\dv_cassette_check.ps1 .\lukas_3
#
# Outputs:
#   Writes one timestamped detailed JSON report directly into the checked
#   cassette folder.
#
# Requirements:
#   - ffprobe (from FFmpeg) in PATH
#   - DVRescue.exe in the parent directory or same directory
#
# ============================================================================

param(
    [Parameter(Mandatory=$true, Position=0)]
    [string]$FolderPath
)

# ---------------------------------------------------------------------------
# Configuration: DV25 PAL Archival Reference Standards
# ---------------------------------------------------------------------------
# Source: IEC 61834, SMPTE 314M, and community archival best practices
# (MediaArea, DVRescue project, University of Texas preservation guides)
#
# DV25 PAL stream specifications:
#   Video: 25 Mbps (constant), 720x576, 25fps, 4:2:0 YUV
#   Audio: PCM 16-bit, either 48kHz (2ch) or 32kHz (4ch stereo)
#   Combined raw data rate: ~3.6 MB/s = ~0.2109 GB/min = ~12.65 GB/hr
#
# MiniDV cassette capacities (SP mode):
#   Standard: 60 minutes (most common consumer tape)
#   Extended: 80 minutes (available from some manufacturers)
#   Maximum: 90 minutes (LP mode, not recommended for archival)

$DV25_VIDEO_BITRATE_BPS       = 28800000       # 28.8 Mbps (video only)
$DV25_TOTAL_BITRATE_BPS       = 29824000       # ~29.8 Mbps (video + audio + overhead)
$DV25_GB_PER_MINUTE           = 0.2109         # GB per minute at native DV25 rate
$DV25_GB_PER_MINUTE_TOLERANCE = 0.015          # +/- 0.015 GB/min tolerance
$DV25_GB_PER_HOUR             = 12.65          # GB per hour reference
$DV25_EXPECTED_WIDTH           = 720
$DV25_EXPECTED_HEIGHT          = 576            # PAL (480 for NTSC)
$DV25_EXPECTED_FPS             = 25             # PAL (29.97 for NTSC)
$DV25_EXPECTED_CODEC           = "dvvideo"
$DV25_VALID_AUDIO_RATES        = @(32000, 48000)
$DV25_EXPECTED_AUDIO_CODEC     = "pcm_s16le"

$MINIDV_SP_STANDARD_MINUTES   = 60             # Standard SP tape
$MINIDV_SP_EXTENDED_MINUTES   = 80             # Extended SP tape
$MINIDV_LP_MINUTES            = 90             # LP mode (not recommended)
$MINIMUM_EXPECTED_MINUTES     = 55             # Warn below this (allows for partial tapes)

function Convert-ToIntOrZero {
    param([object]$Value)
    if ($null -eq $Value) { return 0 }
    $text = [string]$Value
    if ([string]::IsNullOrWhiteSpace($text) -or $text -eq "N/A") { return 0 }
    $parsed = 0
    if ([int]::TryParse($text, [ref]$parsed)) { return $parsed }
    return 0
}

# ---------------------------------------------------------------------------
# Resolve paths
# ---------------------------------------------------------------------------
$FolderPath = (Resolve-Path $FolderPath -ErrorAction Stop).Path
$FolderName = Split-Path $FolderPath -Leaf
$ParentDir  = Split-Path $FolderPath -Parent
$ScriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot   = Split-Path -Parent (Split-Path -Parent $ScriptDir)

# Find DVRescue.exe
$dvrescuePath = $null
foreach ($candidate in @(
    (Join-Path $RepoRoot "DVRescue.exe"),
    (Join-Path $ParentDir "DVRescue.exe"),
    (Join-Path $FolderPath "DVRescue.exe"),
    "DVRescue.exe"
)) {
    if (Test-Path $candidate) {
        $dvrescuePath = (Resolve-Path $candidate).Path
        break
    }
}

if (-not $dvrescuePath) {
    Write-Host "`n  [FATAL] DVRescue.exe not found. Place it in the parent directory or PATH." -ForegroundColor Red
    exit 1
}

# Check ffprobe
try { $null = Get-Command ffprobe -ErrorAction Stop }
catch {
    Write-Host "`n  [FATAL] ffprobe not found in PATH. Install FFmpeg." -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------------------
# Collect AVI files
# ---------------------------------------------------------------------------
$aviFiles = Get-ChildItem -Path $FolderPath -Filter "*.avi" -File | Sort-Object Name
if ($aviFiles.Count -eq 0) {
    Write-Host "`n  [FATAL] No .avi files found in: $FolderPath" -ForegroundColor Red
    exit 1
}

$runStartedAt = Get-Date
$runStamp = $runStartedAt.ToString("yyyyMMdd_HHmmss")
$jsonReportPath = Join-Path $FolderPath "dv_cassette_analysis_$runStamp.json"

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------
$bannerWidth = 72
$line = "=" * $bannerWidth
Write-Host ""
Write-Host "  $line" -ForegroundColor Cyan
Write-Host "   DV CASSETTE ARCHIVAL QUALITY CHECK" -ForegroundColor Cyan
Write-Host "  $line" -ForegroundColor Cyan
Write-Host "   Cassette:    $FolderName" -ForegroundColor White
Write-Host "   Location:    $FolderPath" -ForegroundColor DarkGray
Write-Host "   DVRescue:    $dvrescuePath" -ForegroundColor DarkGray
Write-Host "   Files found: $($aviFiles.Count) AVI files" -ForegroundColor White
Write-Host "   JSON report: $jsonReportPath" -ForegroundColor DarkGray
Write-Host "  $line" -ForegroundColor Cyan
Write-Host ""

# ---------------------------------------------------------------------------
# Phase 1: Stream format validation with ffprobe
# ---------------------------------------------------------------------------
Write-Host "  [PHASE 1] Stream Format Validation (ffprobe)" -ForegroundColor Yellow
Write-Host "  $("-" * 60)" -ForegroundColor DarkGray

$totalDuration   = 0.0
$totalSizeBytes  = [long]0
$formatIssues    = @()
$fileDetails     = @()

$i = 0
foreach ($file in $aviFiles) {
    $i++
    $pct = [math]::Round(($i / $aviFiles.Count) * 100)
    Write-Host "`r  Analyzing [$i/$($aviFiles.Count)] ($pct%) $($file.Name)...          " -NoNewline

    $totalSizeBytes += $file.Length

    # Get format and stream info from ffprobe as JSON. Count readable video
    # frames/packets so truncated captures become visible even when the AVI
    # container itself parses cleanly.
    $probeJson = & ffprobe -v quiet -count_frames -count_packets -print_format json -show_format -show_streams $file.FullName 2>&1 | Out-String
    try {
        $probe = $probeJson | ConvertFrom-Json
    } catch {
        $formatIssues += [PSCustomObject]@{
            File    = $file.Name
            Issue   = "CRITICAL: ffprobe could not parse file"
            Level   = "CRITICAL"
        }
        continue
    }

    $fmt = $probe.format
    $videoStream = $probe.streams | Where-Object { $_.codec_type -eq "video" } | Select-Object -First 1
    $audioStream = $probe.streams | Where-Object { $_.codec_type -eq "audio" } | Select-Object -First 1

    # Duration
    $dur = 0.0
    if ($fmt.duration) { $dur = [double]$fmt.duration }
    $totalDuration += $dur

    $videoFrames = if ($videoStream) {
        $countedFrames = Convert-ToIntOrZero $videoStream.nb_read_frames
        if ($countedFrames -gt 0) { $countedFrames } else { Convert-ToIntOrZero $videoStream.nb_frames }
    } else { 0 }
    $videoPackets = if ($videoStream) { Convert-ToIntOrZero $videoStream.nb_read_packets } else { 0 }
    $pixelFormat = if ($videoStream -and $videoStream.pix_fmt) { [string]$videoStream.pix_fmt } else { "unknown" }

    $detail = [PSCustomObject]@{
        Name       = $file.Name
        Duration   = $dur
        SizeMB     = [math]::Round($file.Length / 1MB, 1)
        VCodec     = if ($videoStream) { $videoStream.codec_name } else { "NONE" }
        Width      = if ($videoStream) { [int]$videoStream.width } else { 0 }
        Height     = if ($videoStream) { [int]$videoStream.height } else { 0 }
        FPS        = if ($videoStream -and $videoStream.r_frame_rate) {
                        $parts = $videoStream.r_frame_rate -split '/'
                        if ($parts.Count -eq 2 -and [int]$parts[1] -ne 0) { [math]::Round([int]$parts[0] / [int]$parts[1], 2) } else { 0 }
                     } else { 0 }
        ACodec     = if ($audioStream) { $audioStream.codec_name } else { "NONE" }
        ASampleRate = if ($audioStream) { [int]$audioStream.sample_rate } else { 0 }
        BitRate    = if ($fmt.bit_rate) { [long]$fmt.bit_rate } else { 0 }
        PixFmt     = $pixelFormat
        VFrames    = $videoFrames
        VPackets   = $videoPackets
    }
    $fileDetails += $detail

    # --- Format checks ---

    # Corrupt media detection. A short clip can be a real complete recording,
    # so duration/frame count alone is not a corruption signal. Only mark
    # files critical when ffprobe cannot identify/decode real media structure.
    $captureFailureReasons = @()
    if (-not $videoStream) {
        $captureFailureReasons += "no video stream"
    } else {
        if ($detail.PixFmt -eq "unknown") {
            $captureFailureReasons += "ffprobe could not determine video pixel format"
        }
        if ($detail.VFrames -le 0 -and $detail.VPackets -le 0) {
            $captureFailureReasons += "no readable video frames/packets"
        }
    }
    if ($captureFailureReasons.Count -gt 0) {
        $formatIssues += [PSCustomObject]@{
            File  = $file.Name
            Issue = "CRITICAL: detectable corrupt media file ($($captureFailureReasons -join '; '))"
            Level = "CRITICAL"
        }
    }

    # Video codec
    if ($detail.VCodec -ne $DV25_EXPECTED_CODEC) {
        $formatIssues += [PSCustomObject]@{ File = $file.Name; Issue = "Video codec is '$($detail.VCodec)' (expected '$DV25_EXPECTED_CODEC')"; Level = "CRITICAL" }
    }

    # Resolution
    if ($detail.Width -ne $DV25_EXPECTED_WIDTH -or $detail.Height -ne $DV25_EXPECTED_HEIGHT) {
        $formatIssues += [PSCustomObject]@{ File = $file.Name; Issue = "Resolution is $($detail.Width)x$($detail.Height) (expected ${DV25_EXPECTED_WIDTH}x${DV25_EXPECTED_HEIGHT} PAL)"; Level = "WARNING" }
    }

    # Frame rate
    if ($detail.FPS -ne $DV25_EXPECTED_FPS) {
        $formatIssues += [PSCustomObject]@{ File = $file.Name; Issue = "Frame rate is $($detail.FPS) fps (expected $DV25_EXPECTED_FPS PAL)"; Level = "WARNING" }
    }

    # Audio codec
    if ($detail.ACodec -ne "NONE" -and $detail.ACodec -ne $DV25_EXPECTED_AUDIO_CODEC) {
        $formatIssues += [PSCustomObject]@{ File = $file.Name; Issue = "Audio codec is '$($detail.ACodec)' (expected '$DV25_EXPECTED_AUDIO_CODEC')"; Level = "WARNING" }
    }

    # Audio sample rate
    if ($detail.ASampleRate -ne 0 -and $detail.ASampleRate -notin $DV25_VALID_AUDIO_RATES) {
        $formatIssues += [PSCustomObject]@{ File = $file.Name; Issue = "Audio sample rate is $($detail.ASampleRate) Hz (expected 32000 or 48000)"; Level = "WARNING" }
    }

    # Per-file bitrate sanity (allow wide tolerance for short clips)
    if ($dur -gt 2.0 -and $detail.BitRate -gt 0) {
        $bitrateDeviation = [math]::Abs($detail.BitRate - $DV25_TOTAL_BITRATE_BPS) / $DV25_TOTAL_BITRATE_BPS
        if ($bitrateDeviation -gt 0.10) {
            $brMbps = [math]::Round($detail.BitRate / 1000000, 1)
            $formatIssues += [PSCustomObject]@{ File = $file.Name; Issue = "Bitrate is ${brMbps} Mbps (expected ~29.8 Mbps, deviation $([math]::Round($bitrateDeviation*100,1))%)"; Level = "WARNING" }
        }
    }

    # Very short clips are allowed. They are noted for review, but not treated
    # as corrupted unless the structural/decode checks above failed.
    if ($dur -lt 1.0) {
        $formatIssues += [PSCustomObject]@{ File = $file.Name; Issue = "Very short clip: $([math]::Round($dur,2))s"; Level = "INFO" }
    }
}
Write-Host "`r  Analyzed $($aviFiles.Count) files.                                          "

# ---------------------------------------------------------------------------
# Phase 1 Results: Duration & Size Summary
# ---------------------------------------------------------------------------
$totalMinutes  = $totalDuration / 60.0
$totalHours    = [math]::Floor($totalDuration / 3600)
$remMinutes    = [math]::Floor(($totalDuration % 3600) / 60)
$remSeconds    = [math]::Round($totalDuration % 60)
$totalGB       = $totalSizeBytes / 1GB
$gbPerMinute   = if ($totalMinutes -gt 0) { $totalGB / $totalMinutes } else { 0 }

Write-Host ""
Write-Host "  --- Duration & Storage ---" -ForegroundColor White
Write-Host "  Total playtime:      ${totalHours}h ${remMinutes}m ${remSeconds}s  ($([math]::Round($totalMinutes, 2)) minutes)" -ForegroundColor White
Write-Host "  Total size:          $([math]::Round($totalGB, 3)) GB" -ForegroundColor White
Write-Host "  Data rate:           $([math]::Round($gbPerMinute, 4)) GB/min" -ForegroundColor White
Write-Host "  Reference rate:      $DV25_GB_PER_MINUTE GB/min (DV25 standard)" -ForegroundColor DarkGray
Write-Host ""

# Duration assessment
if ($totalMinutes -lt $MINIMUM_EXPECTED_MINUTES) {
    Write-Host "  [WARNING] Total playtime is under $MINIMUM_EXPECTED_MINUTES minutes!" -ForegroundColor Yellow
    Write-Host "            A standard MiniDV SP tape holds 60 minutes." -ForegroundColor Yellow
    Write-Host "            This cassette may be incomplete or partially captured." -ForegroundColor Yellow
    Write-Host ""
} elseif ($totalMinutes -ge $MINIDV_SP_STANDARD_MINUTES -and $totalMinutes -le ($MINIDV_SP_STANDARD_MINUTES + 3)) {
    Write-Host "  [OK] Playtime consistent with a full 60-minute SP tape." -ForegroundColor Green
    Write-Host ""
} elseif ($totalMinutes -gt ($MINIDV_SP_STANDARD_MINUTES + 3) -and $totalMinutes -le ($MINIDV_SP_EXTENDED_MINUTES + 3)) {
    Write-Host "  [OK] Playtime consistent with an extended 80-minute SP tape." -ForegroundColor Green
    Write-Host ""
} elseif ($totalMinutes -gt ($MINIDV_SP_EXTENDED_MINUTES + 3)) {
    Write-Host "  [INFO] Playtime exceeds 80 minutes ($([math]::Round($totalMinutes,1)) min)." -ForegroundColor Cyan
    Write-Host "         This may be an LP recording or a full-size DV cassette." -ForegroundColor Cyan
    Write-Host ""
} else {
    Write-Host "  [OK] Playtime: $([math]::Round($totalMinutes, 1)) minutes." -ForegroundColor Green
    Write-Host ""
}

# Data rate assessment
$rateDeviation = [math]::Abs($gbPerMinute - $DV25_GB_PER_MINUTE)
if ($rateDeviation -le $DV25_GB_PER_MINUTE_TOLERANCE) {
    Write-Host "  [OK] Data rate matches DV25 archival standard ($([math]::Round($gbPerMinute, 4)) GB/min)." -ForegroundColor Green
} else {
    Write-Host "  [WARNING] Data rate $([math]::Round($gbPerMinute, 4)) GB/min deviates from DV25 standard ($DV25_GB_PER_MINUTE GB/min)." -ForegroundColor Yellow
    Write-Host "            This may indicate re-encoding, format conversion, or non-DV25 source." -ForegroundColor Yellow
}
Write-Host ""

# Format issues
if ($formatIssues.Count -eq 0) {
    Write-Host "  [OK] All $($aviFiles.Count) files pass format validation." -ForegroundColor Green
} else {
    $criticals = @($formatIssues | Where-Object { $_.Level -eq "CRITICAL" })
    $warnings  = @($formatIssues | Where-Object { $_.Level -eq "WARNING" })
    $infos     = @($formatIssues | Where-Object { $_.Level -eq "INFO" })

    if ($criticals.Count -gt 0) {
        Write-Host "  [CRITICAL] $($criticals.Count) critical format issue(s):" -ForegroundColor Red
        foreach ($issue in $criticals) {
            Write-Host "    ! $($issue.File): $($issue.Issue)" -ForegroundColor Red
        }
    }
    if ($warnings.Count -gt 0) {
        Write-Host "  [WARNING] $($warnings.Count) format warning(s):" -ForegroundColor Yellow
        foreach ($issue in $warnings) {
            Write-Host "    ~ $($issue.File): $($issue.Issue)" -ForegroundColor Yellow
        }
    }
    if ($infos.Count -gt 0) {
        Write-Host "  [INFO] $($infos.Count) note(s):" -ForegroundColor DarkGray
        foreach ($issue in $infos) {
            Write-Host "    - $($issue.File): $($issue.Issue)" -ForegroundColor DarkGray
        }
    }
}
Write-Host ""

# ---------------------------------------------------------------------------
# Phase 2: DV Stream Integrity Analysis (DVRescue)
# ---------------------------------------------------------------------------
Write-Host "  $("-" * 60)" -ForegroundColor DarkGray
Write-Host "  [PHASE 2] DV Stream Integrity Analysis (DVRescue)" -ForegroundColor Yellow
Write-Host "  $("-" * 60)" -ForegroundColor DarkGray
Write-Host "  Scanning all frames for errors, concealment, and timecode issues..." -ForegroundColor DarkGray
Write-Host ""

$dvIssues           = @()   # Issues found
$totalFrames        = 0
$totalErrorFrames   = 0
$totalVideoSTAErrs  = 0
$totalAudioDataErrs = 0
$totalConcealedFrames = 0
$totalMidContentErrors = 0
$totalMidContentConcealed = 0
$totalTailErrors    = 0
$totalTailConcealed = 0
$timecodeJumps      = 0
$filesWithErrors    = @()
$filesClean         = 0
$perFileStats       = @()  # Per-file structured stats for ranking

$i = 0
foreach ($file in $aviFiles) {
    $i++
    $pct = [math]::Round(($i / $aviFiles.Count) * 100)
    Write-Host "`r  DVRescue [$i/$($aviFiles.Count)] ($pct%) $($file.Name)...          " -NoNewline

    # Run DVRescue and capture XML output (read-only, no output files)
    $xmlOutput = & $dvrescuePath $file.FullName -v 9 2>&1 | Out-String

    # Parse XML
    try {
        $xml = [xml]$xmlOutput
    } catch {
        $dvIssues += [PSCustomObject]@{
            File  = $file.Name
            Issue = "DVRescue XML parse error - file may be corrupt"
            Level = "CRITICAL"
        }
        continue
    }

    $nsMgr = New-Object Xml.XmlNamespaceManager($xml.NameTable)
    $nsMgr.AddNamespace('d', 'https://mediaarea.net/dvrescue')

    $media = $xml.SelectSingleNode('//d:media', $nsMgr)
    if (-not $media) { continue }

    $sections = $media.SelectNodes('d:frames', $nsMgr)
    $sectionCount = $sections.Count
    $fileVideoSTA = 0
    $fileAudioData = 0
    $fileConcealed = 0
    $fileErrorFrames = 0
    $fileFrameCount = 0
    $fileHasContentErrors = $false
    $fileMidContentErrors = 0
    $fileMidContentConcealed = 0
    $fileTailErrors = 0
    $fileTailConcealed = 0

    # Tail zone: errors in the last 2% of a file or last 0.5s (whichever is
    # larger) are classified as recording-boundary artifacts, not tape damage.
    # DV cameras often leave corrupted frames when stopping a recording.
    $fileDurForTail = [double]($fileDetails | Where-Object { $_.Name -eq $file.Name }).Duration
    $tailThresholdPct = $fileDurForTail * 0.98  # last 2% of file
    $tailThresholdSec = $fileDurForTail - 0.5   # last 0.5 seconds
    $tailCutoff = [math]::Min($tailThresholdPct, $tailThresholdSec)
    if ($tailCutoff -lt 0) { $tailCutoff = $fileDurForTail }

    # Extract tape timecodes from all frames (first, last, and error frames)
    $allFrames = $media.SelectNodes('.//d:frame', $nsMgr)
    $tapeStartTC  = $null
    $tapeStartRDT = $null
    $tapeEndTC    = $null
    $tapeEndRDT   = $null
    $errorTCs     = @()  # tape timecodes where errors occur

    if ($allFrames.Count -gt 0) {
        $firstFrame = $allFrames[0]
        $lastFrame  = $allFrames[$allFrames.Count - 1]
        $tapeStartTC  = $firstFrame.tc
        $tapeStartRDT = $firstFrame.rdt
        $tapeEndTC    = $lastFrame.tc
        $tapeEndRDT   = $lastFrame.rdt
    }

    foreach ($section in $sections) {
        $count     = if ($section.count) { [int]$section.count } else { 0 }
        $speed     = $section.speed
        $audioRate = $section.audio_rate
        $fileFrameCount += $count

        $isPreroll = ($speed -eq "127" -or (-not $audioRate -and $speed))

        # Count timecode discontinuities (each <frames> section = a segment boundary)
        # Multiple sections in one file means timecode jumps / recording breaks
        if ($sectionCount -gt 2) {
            # More than the typical preroll+content split
        }

        # Examine individual frames with errors
        $errorFrames = $section.SelectNodes('d:frame[d:dseq]', $nsMgr)

        foreach ($frame in $errorFrames) {
            $dseqs = $frame.SelectNodes('d:dseq', $nsMgr)
            $frameHasSTA = $false
            $frameHasAud = $false
            $staCount = 0
            $audCount = 0

            foreach ($dseq in $dseqs) {
                $stas = $dseq.SelectNodes('d:sta', $nsMgr)
                $auds = $dseq.SelectNodes('d:aud', $nsMgr)

                foreach ($sta in $stas) {
                    $n = if ($sta.n) { [int]$sta.n } else { 0 }
                    $staCount += $n
                    $frameHasSTA = $true
                }
                foreach ($aud in $auds) {
                    $n = if ($aud.n) { [int]$aud.n } else { 0 }
                    $audCount += $n
                    $frameHasAud = $true
                }
            }

            if ($isPreroll) {
                # Pre-roll errors are expected (deck seeking, no audio track yet)
                # We skip these -- they do not represent real content damage
                continue
            }

            # Content-area errors
            if ($frameHasSTA -or $frameHasAud) {
                $fileErrorFrames++
                $fileHasContentErrors = $true

                if ($frameHasSTA) { $fileVideoSTA += $staCount }
                if ($frameHasAud) { $fileAudioData += $audCount }

                # Check if fully concealed (all 1500 blocks for PAL)
                $isConcealed = ($staCount -ge 1500)
                if ($isConcealed) { $fileConcealed++ }

                # Classify: tail zone (recording boundary) vs mid-content (real damage)
                $ptsSec = 0.0
                if ($frame.pts) {
                    $ptsParts = $frame.pts -split ':'
                    $ptsSec = [int]$ptsParts[0] * 3600 + [int]$ptsParts[1] * 60 + [double]$ptsParts[2]
                }
                $isTail = ($ptsSec -ge $tailCutoff)

                if ($isTail) {
                    $fileTailErrors++
                    if ($isConcealed) { $fileTailConcealed++ }
                } else {
                    $fileMidContentErrors++
                    if ($isConcealed) { $fileMidContentConcealed++ }
                }

                # Capture tape timecode of this error frame
                $errTC = $frame.tc
                $errRDT = $frame.rdt
                if ($errTC) {
                    $errorTCs += [PSCustomObject]@{ TC = $errTC; RDT = $errRDT; PTS = $frame.pts; IsTail = $isTail }
                }
            }
        }
    }

    $totalFrames += $fileFrameCount

    # Timecode jumps: if more than 2 sections (preroll + content), there are breaks
    if ($sectionCount -gt 2) {
        $jumps = $sectionCount - 2  # subtract the typical preroll + main content
        $timecodeJumps += $jumps
        $dvIssues += [PSCustomObject]@{
            File  = $file.Name
            Issue = "$jumps timecode break(s) detected (recording interruptions on tape)"
            Level = "INFO"
        }
    }

    # Store per-file stats for ranking (all files, not just errored ones)
    $errPct = if ($fileFrameCount -gt 0) { [math]::Round(($fileErrorFrames / $fileFrameCount) * 100, 2) } else { 0 }
    # Deduplicate error TCs (many error frames may share the same TC)
    $uniqueErrorTCs = @($errorTCs | Select-Object TC, RDT, PTS -Unique)

    $isTailOnly = ($fileMidContentErrors -eq 0 -and $fileTailErrors -gt 0)

    $perFileStats += [PSCustomObject]@{
        Name                = $file.Name
        FullPath            = $file.FullName
        FrameCount          = $fileFrameCount
        ErrorFrames         = $fileErrorFrames
        ErrorPct            = $errPct
        VideoSTA            = $fileVideoSTA
        AudioData           = $fileAudioData
        Concealed           = $fileConcealed
        MidContentErrors    = $fileMidContentErrors
        MidContentConcealed = $fileMidContentConcealed
        TailErrors          = $fileTailErrors
        TailConcealed       = $fileTailConcealed
        IsTailOnly          = $isTailOnly
        TapeStartTC         = $tapeStartTC
        TapeStartRDT        = $tapeStartRDT
        TapeEndTC           = $tapeEndTC
        TapeEndRDT          = $tapeEndRDT
        ErrorTCs            = $uniqueErrorTCs
        FreezeEvents        = 0       # filled in Phase 3
        FreezeSec           = 0.0     # filled in Phase 3
        BlackEvents         = 0       # filled in Phase 3
        BlackSec            = 0.0     # filled in Phase 3
        SilenceEvents       = 0       # filled in Phase 3
        SilenceSec          = 0.0     # filled in Phase 3
    }

    if ($fileHasContentErrors) {
        $totalErrorFrames += $fileErrorFrames
        $totalVideoSTAErrs += $fileVideoSTA
        $totalAudioDataErrs += $fileAudioData
        $totalConcealedFrames += $fileConcealed
        $totalMidContentErrors += $fileMidContentErrors
        $totalMidContentConcealed += $fileMidContentConcealed
        $totalTailErrors += $fileTailErrors
        $totalTailConcealed += $fileTailConcealed

        $filesWithErrors += $file.Name

        # Severity: tail-only errors are downgraded to INFO
        # Mid-content concealed frames or >5% error rate = CRITICAL
        # Mid-content errors without concealment = WARNING
        # Tail-only errors = INFO (recording boundary artifact)
        if ($isTailOnly) {
            $severity = "INFO"
        } elseif ($fileMidContentConcealed -gt 0 -or $errPct -gt 5.0) {
            $severity = "CRITICAL"
        } else {
            $severity = "WARNING"
        }

        $msg = "$fileErrorFrames error frame(s) out of $fileFrameCount ($errPct%)"
        if ($fileVideoSTA -gt 0) { $msg += ", Video STA errors: $fileVideoSTA" }
        if ($fileAudioData -gt 0) { $msg += ", Audio errors: $fileAudioData" }
        if ($fileConcealed -gt 0) { $msg += ", FULLY CONCEALED: $fileConcealed" }
        if ($isTailOnly) { $msg += " [TAIL ONLY - recording boundary]" }
        elseif ($fileTailErrors -gt 0) { $msg += " [$fileMidContentErrors mid-content + $fileTailErrors tail]" }

        $dvIssues += [PSCustomObject]@{
            File  = $file.Name
            Issue = $msg
            Level = $severity
        }
    } else {
        $filesClean++
    }
}
Write-Host "`r  DVRescue complete: scanned $($aviFiles.Count) files, $totalFrames total frames.          "
Write-Host ""

# ---------------------------------------------------------------------------
# Phase 2 Results
# ---------------------------------------------------------------------------
Write-Host "  --- DV Stream Integrity Summary ---" -ForegroundColor White
Write-Host "  Total frames scanned:    $totalFrames" -ForegroundColor White
Write-Host "  Files clean:             $filesClean / $($aviFiles.Count)" -ForegroundColor $(if ($filesClean -eq $aviFiles.Count) { "Green" } else { "Yellow" })
Write-Host "  Files with errors:       $($filesWithErrors.Count) / $($aviFiles.Count)" -ForegroundColor $(if ($filesWithErrors.Count -eq 0) { "Green" } else { "Red" })
Write-Host "  Error frames (total):    $totalErrorFrames" -ForegroundColor $(if ($totalErrorFrames -eq 0) { "Green" } else { "Yellow" })
Write-Host "    Mid-content errors:    $totalMidContentErrors" -ForegroundColor $(if ($totalMidContentErrors -eq 0) { "Green" } else { "Red" })
Write-Host "    Tail errors (boundary): $totalTailErrors" -ForegroundColor $(if ($totalTailErrors -eq 0) { "Green" } else { "DarkGray" })
Write-Host "  Video STA errors:        $totalVideoSTAErrs" -ForegroundColor $(if ($totalVideoSTAErrs -eq 0) { "Green" } else { "Yellow" })
Write-Host "  Audio data errors:       $totalAudioDataErrs" -ForegroundColor $(if ($totalAudioDataErrs -eq 0) { "Green" } else { "Yellow" })
Write-Host "  Fully concealed frames:  $totalConcealedFrames (mid-content: $totalMidContentConcealed, tail: $totalTailConcealed)" -ForegroundColor $(if ($totalMidContentConcealed -eq 0) { $(if ($totalConcealedFrames -eq 0) { "Green" } else { "DarkGray" }) } else { "Red" })
Write-Host "  Timecode breaks:         $timecodeJumps" -ForegroundColor $(if ($timecodeJumps -eq 0) { "Green" } else { "Cyan" })
Write-Host ""

# Detail any issues
$dvCriticals = @($dvIssues | Where-Object { $_.Level -eq "CRITICAL" })
$dvWarnings  = @($dvIssues | Where-Object { $_.Level -eq "WARNING" })
$dvInfos     = @($dvIssues | Where-Object { $_.Level -eq "INFO" })

if ($dvCriticals.Count -gt 0) {
    Write-Host "  [CRITICAL] $($dvCriticals.Count) critical DV stream issue(s):" -ForegroundColor Red
    foreach ($issue in $dvCriticals) {
        Write-Host "    ! $($issue.File): $($issue.Issue)" -ForegroundColor Red
    }
    Write-Host ""
}
if ($dvWarnings.Count -gt 0) {
    Write-Host "  [WARNING] $($dvWarnings.Count) DV stream warning(s):" -ForegroundColor Yellow
    foreach ($issue in $dvWarnings) {
        Write-Host "    ~ $($issue.File): $($issue.Issue)" -ForegroundColor Yellow
    }
    Write-Host ""
}
if ($dvInfos.Count -gt 0) {
    Write-Host "  [INFO] $($dvInfos.Count) note(s):" -ForegroundColor DarkGray
    foreach ($issue in $dvInfos) {
        Write-Host "    - $($issue.File): $($issue.Issue)" -ForegroundColor DarkGray
    }
    Write-Host ""
}

# ---------------------------------------------------------------------------
# Phase 3: Visual Anomaly Detection (ffmpeg filters, read-only)
# ---------------------------------------------------------------------------
# Uses ffmpeg filter pipeline to detect anomalies that are invisible at the
# DV stream level but visible on playback. All processing uses -f null -
# which decodes to memory and discards output. NOTHING is written to disk.
#
# Checks performed:
#   1. freezedetect  - Frozen/stuck frames (same image >0.5s = capture stall,
#                      head clog with partial freeze, or tape damage)
#   2. blackdetect   - Solid black sequences >0.5s (signal loss, blank tape
#                      sections, or head dropout)
#   3. silencedetect - Audio silence >1.0s (audio dropout, muted sections)
#
# Filter thresholds:
#   freezedetect: noise=-60dB (very strict pixel match), duration=0.5s
#   blackdetect:  pixel_threshold=0.10, picture_threshold=0.98, duration=0.5s
#   silencedetect: noise=-50dB, duration=1.0s
# ---------------------------------------------------------------------------
Write-Host "  $("-" * 60)" -ForegroundColor DarkGray
Write-Host "  [PHASE 3] Visual Anomaly Detection (ffmpeg filters)" -ForegroundColor Yellow
Write-Host "  $("-" * 60)" -ForegroundColor DarkGray
Write-Host "  Full frame decode: checking for frozen frames, black frames," -ForegroundColor DarkGray
Write-Host "  and audio silence. This is read-only (output to null)." -ForegroundColor DarkGray
Write-Host ""

$visualIssues    = @()
$totalFreezes    = 0
$totalPhotoMode  = 0
$totalBlacks     = 0
$totalSilences   = 0
$freezeTotalSec  = 0.0
$photoModeTotalSec = 0.0
$blackTotalSec   = 0.0
$silenceTotalSec = 0.0
$filesWithVisual = 0

$i = 0
foreach ($file in $aviFiles) {
    $i++
    $pct = [math]::Round(($i / $aviFiles.Count) * 100)
    Write-Host "`r  Scanning [$i/$($aviFiles.Count)] ($pct%) $($file.Name)...          " -NoNewline

    # Run ffmpeg with combined video + audio filter chains, output to null
    # -nostdin prevents ffmpeg from reading stdin (important for batch)
    # Video filters: freezedetect then blackdetect (chained)
    # Audio filter: silencedetect (separate filtergraph on audio stream)
    $ffOutput = & ffmpeg -nostdin -i $file.FullName `
        -vf "freezedetect=n=-60dB:d=0.5,blackdetect=d=0.5:pix_th=0.10:pic_th=0.98" `
        -af "silencedetect=n=-50dB:d=1.0" `
        -f null - 2>&1 | Out-String
    $ffmpegExitCode = $LASTEXITCODE

    if ($ffmpegExitCode -ne 0) {
        $decodeError = "FFmpeg full decode failed with exit code $ffmpegExitCode"
        $invalidLine = ($ffOutput -split "`r?`n" | Where-Object { $_ -match 'Invalid data|Cannot determine format|Could not find codec parameters|Nothing was written' } | Select-Object -First 1)
        if ($invalidLine) { $decodeError += ": $invalidLine" }
        $visualIssues += [PSCustomObject]@{
            File  = $file.Name
            Type  = "DECODE"
            Issue = $decodeError
            Level = "CRITICAL"
        }
    }

    # --- Parse freeze detections ---
    $freezeMatches = [regex]::Matches($ffOutput, 'freeze_start:\s*([\d.]+)')
    $freezeEndMatches = [regex]::Matches($ffOutput, 'freeze_duration:\s*([\d.]+)')

    $fileFreezes = @()
    $filePhotoModes = @()
    $fileDuration = [double]($fileDetails | Where-Object { $_.Name -eq $file.Name }).Duration
    for ($fi = 0; $fi -lt $freezeMatches.Count; $fi++) {
        $fStart = [double]$freezeMatches[$fi].Groups[1].Value
        $runsToEnd = ($fi -ge $freezeEndMatches.Count)  # no freeze_duration = runs to end of file
        $fDur   = if (-not $runsToEnd) {
                      [double]$freezeEndMatches[$fi].Groups[1].Value
                  } else {
                      [math]::Round($fileDuration - $fStart, 2)
                  }

        # Detect MiniDV camera photo mode: some camcorders record a single
        # frame for several seconds when using the photo button. Signature:
        #   - Freeze runs to end of file (no freeze_end emitted by ffmpeg)
        #   - Freeze duration is 5-10 seconds
        # These are intentional, not capture errors or tape damage.
        $isPhotoMode = $runsToEnd -and ($fDur -ge 5.0) -and ($fDur -le 10.0)

        # Convert to timecode for human readability
        $fStartTC = "{0:d2}:{1:d2}:{2:00.0}" -f [int][math]::Floor($fStart/3600), [int][math]::Floor(($fStart%3600)/60), ($fStart%60)

        $freezeObj = [PSCustomObject]@{
            Start       = $fStart
            Duration    = $fDur
            StartTC     = $fStartTC
            IsPhotoMode = $isPhotoMode
        }

        if ($isPhotoMode) {
            $filePhotoModes += $freezeObj
            $photoModeTotalSec += $fDur
        } else {
            $fileFreezes += $freezeObj
            $freezeTotalSec += $fDur
        }
    }

    # --- Parse black detections ---
    $blackMatches = [regex]::Matches($ffOutput, 'black_start:([\d.]+)\s+black_end:([\d.]+)\s+black_duration:([\d.]+)')

    $fileBlacks = @()
    foreach ($bm in $blackMatches) {
        $bStart = [double]$bm.Groups[1].Value
        $bEnd   = [double]$bm.Groups[2].Value
        $bDur   = [double]$bm.Groups[3].Value
        $bStartTC = "{0:d2}:{1:d2}:{2:00.0}" -f [int][math]::Floor($bStart/3600), [int][math]::Floor(($bStart%3600)/60), ($bStart%60)

        $fileBlacks += [PSCustomObject]@{
            Start    = $bStart
            End      = $bEnd
            Duration = $bDur
            StartTC  = $bStartTC
        }
        $blackTotalSec += $bDur
    }

    # --- Parse silence detections ---
    $silStartMatches = [regex]::Matches($ffOutput, 'silence_start:\s*([\d.]+)')
    $silEndMatches   = [regex]::Matches($ffOutput, 'silence_end:\s*([\d.]+)\s*\|\s*silence_duration:\s*([\d.]+)')

    $fileSilences = @()
    for ($si = 0; $si -lt $silStartMatches.Count; $si++) {
        $sStart = [double]$silStartMatches[$si].Groups[1].Value
        $sDur   = if ($si -lt $silEndMatches.Count) { [double]$silEndMatches[$si].Groups[2].Value } else { 0 }
        $sStartTC = "{0:d2}:{1:d2}:{2:00.0}" -f [int][math]::Floor($sStart/3600), [int][math]::Floor(($sStart%3600)/60), ($sStart%60)

        $fileSilences += [PSCustomObject]@{
            Start    = $sStart
            Duration = $sDur
            StartTC  = $sStartTC
        }
        $silenceTotalSec += $sDur
    }

    # --- Report per file ---
    $totalFreezes  += $fileFreezes.Count
    $totalPhotoMode += $filePhotoModes.Count
    $totalBlacks   += $fileBlacks.Count
    $totalSilences += $fileSilences.Count

    $fileHasVisualIssues = $false

    # Camera photo mode events (not anomalies)
    if ($filePhotoModes.Count -gt 0) {
        foreach ($p in $filePhotoModes) {
            $visualIssues += [PSCustomObject]@{
                File  = $file.Name
                Type  = "PHOTO"
                Issue = "Camera photo mode at $($p.StartTC) for $([math]::Round($p.Duration, 2))s (not an error)"
                Level = "INFO"
            }
        }
    }

    # Real freeze events (anomalies)
    if ($fileFreezes.Count -gt 0) {
        $fileHasVisualIssues = $true
        foreach ($f in $fileFreezes) {
            $severity = if ($f.Duration -ge 5.0) { "CRITICAL" } elseif ($f.Duration -ge 2.0) { "WARNING" } else { "INFO" }
            $visualIssues += [PSCustomObject]@{
                File  = $file.Name
                Type  = "FROZEN"
                Issue = "Frozen frame at $($f.StartTC) for $([math]::Round($f.Duration, 2))s"
                Level = $severity
            }
        }
    }

    if ($fileBlacks.Count -gt 0) {
        $fileHasVisualIssues = $true
        foreach ($b in $fileBlacks) {
            # Black at the very start (first 0.5s) or very end is often normal (tape start/stop)
            $isEdge = ($b.Start -lt 0.5) -or (($b.End + 0.5) -ge ([double]($fileDetails | Where-Object { $_.Name -eq $file.Name }).Duration))
            $severity = if ($b.Duration -ge 5.0 -and -not $isEdge) { "WARNING" } elseif ($isEdge) { "INFO" } else { "INFO" }
            $visualIssues += [PSCustomObject]@{
                File  = $file.Name
                Type  = "BLACK"
                Issue = "Black sequence at $($b.StartTC) for $([math]::Round($b.Duration, 2))s$(if ($isEdge) { ' (clip edge)' })"
                Level = $severity
            }
        }
    }

    if ($fileSilences.Count -gt 0) {
        $fileHasVisualIssues = $true
        foreach ($s in $fileSilences) {
            $severity = if ($s.Duration -ge 5.0) { "WARNING" } else { "INFO" }
            $visualIssues += [PSCustomObject]@{
                File  = $file.Name
                Type  = "SILENCE"
                Issue = "Audio silence at $($s.StartTC) for $([math]::Round($s.Duration, 2))s"
                Level = $severity
            }
        }
    }

    if ($fileHasVisualIssues) { $filesWithVisual++ }

    # Update per-file stats with Phase 3 data
    $pfs = $perFileStats | Where-Object { $_.Name -eq $file.Name }
    if ($pfs) {
        $pfs.FreezeEvents  = $fileFreezes.Count  # excludes photo mode
        $pfs.FreezeSec     = ($fileFreezes | Measure-Object -Property Duration -Sum).Sum
        if (-not $pfs.FreezeSec) { $pfs.FreezeSec = 0 }
        # Photo mode events tracked but don't count as anomalies
        $pfs | Add-Member -NotePropertyName PhotoEvents -NotePropertyValue $filePhotoModes.Count -Force
        $pfs | Add-Member -NotePropertyName PhotoSec -NotePropertyValue (($filePhotoModes | Measure-Object -Property Duration -Sum).Sum) -Force
        if (-not $pfs.PhotoSec) { $pfs.PhotoSec = 0 }
        $pfs.BlackEvents   = $fileBlacks.Count
        $pfs.BlackSec      = ($fileBlacks | Measure-Object -Property Duration -Sum).Sum
        if (-not $pfs.BlackSec) { $pfs.BlackSec = 0 }
        $pfs.SilenceEvents = $fileSilences.Count
        $pfs.SilenceSec    = ($fileSilences | Measure-Object -Property Duration -Sum).Sum
        if (-not $pfs.SilenceSec) { $pfs.SilenceSec = 0 }
    }
}
Write-Host "`r  Visual scan complete: decoded $($aviFiles.Count) files.                                    "
Write-Host ""

# ---------------------------------------------------------------------------
# Phase 3 Results
# ---------------------------------------------------------------------------
Write-Host "  --- Visual Anomaly Summary ---" -ForegroundColor White
Write-Host "  Frozen frame events:     $totalFreezes ($([math]::Round($freezeTotalSec, 1))s total)" -ForegroundColor $(if ($totalFreezes -eq 0) { "Green" } else { "Yellow" })
if ($totalPhotoMode -gt 0) {
    Write-Host "  Camera photo mode:       $totalPhotoMode ($([math]::Round($photoModeTotalSec, 1))s total) -- not errors" -ForegroundColor DarkGray
}
Write-Host "  Black sequence events:   $totalBlacks ($([math]::Round($blackTotalSec, 1))s total)" -ForegroundColor $(if ($totalBlacks -eq 0) { "Green" } else { "Cyan" })
Write-Host "  Audio silence events:    $totalSilences ($([math]::Round($silenceTotalSec, 1))s total)" -ForegroundColor $(if ($totalSilences -eq 0) { "Green" } else { "Cyan" })
Write-Host "  Files with anomalies:    $filesWithVisual / $($aviFiles.Count)" -ForegroundColor $(if ($filesWithVisual -eq 0) { "Green" } else { "Yellow" })
Write-Host ""

$visCriticals = @($visualIssues | Where-Object { $_.Level -eq "CRITICAL" })
$visWarnings  = @($visualIssues | Where-Object { $_.Level -eq "WARNING" })
$visInfos     = @($visualIssues | Where-Object { $_.Level -eq "INFO" })

if ($visCriticals.Count -gt 0) {
    Write-Host "  [CRITICAL] $($visCriticals.Count) critical visual anomaly(s):" -ForegroundColor Red
    foreach ($issue in $visCriticals) {
        Write-Host "    ! [$($issue.Type)] $($issue.File): $($issue.Issue)" -ForegroundColor Red
    }
    Write-Host ""
}
if ($visWarnings.Count -gt 0) {
    Write-Host "  [WARNING] $($visWarnings.Count) visual warning(s):" -ForegroundColor Yellow
    foreach ($issue in $visWarnings) {
        Write-Host "    ~ [$($issue.Type)] $($issue.File): $($issue.Issue)" -ForegroundColor Yellow
    }
    Write-Host ""
}
if ($visInfos.Count -gt 0) {
    Write-Host "  [INFO] $($visInfos.Count) visual note(s):" -ForegroundColor DarkGray
    foreach ($issue in $visInfos) {
        Write-Host "    - [$($issue.Type)] $($issue.File): $($issue.Issue)" -ForegroundColor DarkGray
    }
    Write-Host ""
}

if ($visualIssues.Count -eq 0) {
    Write-Host "  [OK] No frozen frames, black sequences, or audio silence detected." -ForegroundColor Green
    Write-Host ""
}

# ---------------------------------------------------------------------------
# Top 3 Most Impacted Files
# ---------------------------------------------------------------------------
# Composite impact score weights:
#   - Fully concealed frames are the worst (complete data loss)
#   - Frozen frame duration indicates visible playback issues
#   - Error frame percentage shows overall stream damage
#   - Raw STA error count reflects granular block-level damage
#
# Score = (Concealed * 1000) + (FreezeSec * 100) + (ErrorPct * 50)
#       + (VideoSTA / 100) + (BlackSec * 30) + (SilenceSec * 10)
# ---------------------------------------------------------------------------
$impactedFiles = $perFileStats | Where-Object {
    $_.ErrorFrames -gt 0 -or $_.FreezeEvents -gt 0 -or $_.BlackEvents -gt 0 -or $_.SilenceEvents -gt 0
} | ForEach-Object {
    $score = ($_.Concealed * 1000) + ($_.FreezeSec * 100) + ($_.ErrorPct * 50) + ($_.VideoSTA / 100) + ($_.BlackSec * 30) + ($_.SilenceSec * 10)
    $_ | Add-Member -NotePropertyName ImpactScore -NotePropertyValue ([math]::Round($score, 1)) -PassThru
} | Sort-Object ImpactScore -Descending

$top3 = @($impactedFiles | Select-Object -First 3)

if ($top3.Count -gt 0) {
    Write-Host "  $("-" * 60)" -ForegroundColor DarkGray
    Write-Host "  [TOP $($top3.Count) MOST IMPACTED FILES] Review these for visible quality issues:" -ForegroundColor Magenta
    Write-Host "  $("-" * 60)" -ForegroundColor DarkGray
    Write-Host ""

    # Build cumulative cassette position lookup from file durations (in tape order)
    # The files are named chronologically and captured sequentially from the tape,
    # so the real cassette position = sum of all preceding file durations.
    # DV timecodes are NOT reliable for this because they reset when the tape
    # is ejected/re-inserted into the camera.
    $cassettePositions = @{}
    $cumulativeSeconds = 0.0
    foreach ($fd in ($fileDetails | Sort-Object Name)) {
        $cassettePositions[$fd.Name] = $cumulativeSeconds
        $cumulativeSeconds += $fd.Duration
    }

    # Helper: format seconds as "min X" or "Xh Ym"
    function Format-CassetteMin($seconds) {
        $totalMin = [math]::Round($seconds / 60.0, 1)
        if ($totalMin -ge 60) {
            $h = [int][math]::Floor($totalMin / 60)
            $m = [math]::Round($totalMin % 60, 1)
            return "${h}h ${m}m"
        }
        return "min $totalMin"
    }

    $rank = 0
    foreach ($tf in $top3) {
        $rank++
        $durInfo = $fileDetails | Where-Object { $_.Name -eq $tf.Name }
        $clipDur = if ($durInfo) { [math]::Round($durInfo.Duration, 1) } else { 0 }
        $clipSec = [int]$clipDur

        # Real cassette position from cumulative durations
        $cassetteStartSec = $cassettePositions[$tf.Name]
        $cassetteEndSec   = $cassetteStartSec + $clipDur

        Write-Host "  #$rank  $($tf.Name)" -ForegroundColor White
        Write-Host "        Path:       $($tf.FullPath)" -ForegroundColor DarkGray
        Write-Host "        Clip length: ${clipSec}s" -ForegroundColor DarkGray

        # The key info: where on the cassette
        $startLabel = Format-CassetteMin $cassetteStartSec
        $endLabel   = Format-CassetteMin $cassetteEndSec
        Write-Host "        ON CASSETTE: $startLabel -> $endLabel" -ForegroundColor Cyan

        if ($tf.TapeStartRDT) {
            Write-Host "        Recorded:    $($tf.TapeStartRDT)" -ForegroundColor DarkGray
        }

        # Where exactly are the errors on the cassette
        if ($tf.ErrorTCs -and $tf.ErrorTCs.Count -gt 0) {
            # Use PTS (position within file) to compute cassette error position
            $errPositions = @($tf.ErrorTCs | ForEach-Object {
                $pts = $_.PTS
                if ($pts) {
                    # PTS format is "HH:MM:SS.ffffff"
                    $ptsParts = $pts -split ':'
                    $ptsSec = [int]$ptsParts[0] * 3600 + [int]$ptsParts[1] * 60 + [double]$ptsParts[2]
                    $errCassetteSec = $cassetteStartSec + $ptsSec
                    Format-CassetteMin $errCassetteSec
                }
            }) | Select-Object -Unique
            if ($errPositions.Count -gt 0) {
                if ($errPositions.Count -le 5) {
                    Write-Host "        ERRORS AT:   $($errPositions -join ', ') on cassette" -ForegroundColor Red
                } else {
                    $shown = ($errPositions | Select-Object -First 3) -join ', '
                    Write-Host "        ERRORS AT:   $shown ... +$($errPositions.Count - 3) more on cassette" -ForegroundColor Red
                }
            }
        }

        # Impact summary
        if ($tf.IsTailOnly) {
            Write-Host "        TAIL ONLY:   All $($tf.ErrorFrames) errors are at clip end (recording boundary)" -ForegroundColor DarkGray
            if ($tf.Concealed -gt 0) {
                Write-Host "        Concealed:   $($tf.Concealed) frame(s) -- camera stop artifact, not tape damage" -ForegroundColor DarkGray
            }
        } else {
            if ($tf.ErrorFrames -gt 0) {
                $errColor = if ($tf.MidContentConcealed -gt 0) { "Red" } else { "Yellow" }
                $midLabel = if ($tf.TailErrors -gt 0) { " ($($tf.MidContentErrors) mid-content + $($tf.TailErrors) tail)" } else { "" }
                Write-Host "        Impact:      $($tf.ErrorFrames) error frames ($($tf.ErrorPct)%)$midLabel" -ForegroundColor $errColor
            }
            if ($tf.MidContentConcealed -gt 0) {
                Write-Host "        Concealed:   $($tf.MidContentConcealed) mid-content + $($tf.TailConcealed) tail" -ForegroundColor Red
            } elseif ($tf.Concealed -gt 0) {
                Write-Host "        Concealed:   $($tf.Concealed) frame(s) (tail only)" -ForegroundColor DarkGray
            }
        }
        if ($tf.FreezeEvents -gt 0) {
            Write-Host "        Frozen:      $($tf.FreezeEvents) event(s), $([math]::Round($tf.FreezeSec, 2))s" -ForegroundColor Yellow
        }

        Write-Host "        Score:       $($tf.ImpactScore)" -ForegroundColor DarkGray
        Write-Host ""
    }

    if ($impactedFiles.Count -gt 3) {
        Write-Host "  ... and $($impactedFiles.Count - 3) more file(s) with issues (not shown)." -ForegroundColor DarkGray
        Write-Host ""
    }
}

# ---------------------------------------------------------------------------
# Final Verdict
# ---------------------------------------------------------------------------
Write-Host "  $line" -ForegroundColor Cyan
$allCriticals = @($formatIssues | Where-Object { $_.Level -eq "CRITICAL" }) + $dvCriticals + $visCriticals
$allWarnings  = @($formatIssues | Where-Object { $_.Level -eq "WARNING" }) + $dvWarnings + $visWarnings

$verdict = "PASS"
$verdictColor = "Green"
$verdictIcon = [char]0x2714  # checkmark

if ($allWarnings.Count -gt 0) {
    $verdict = "PASS WITH WARNINGS"
    $verdictColor = "Yellow"
    $verdictIcon = "~"
}
if ($totalMinutes -lt $MINIMUM_EXPECTED_MINUTES) {
    $verdict = "PASS WITH WARNINGS"
    $verdictColor = "Yellow"
    $verdictIcon = "~"
}
# Only mid-content concealed frames or criticals trigger FAIL.
# Tail-only concealed frames (recording boundaries) do not.
if ($allCriticals.Count -gt 0 -or $totalMidContentConcealed -gt 0) {
    $verdict = "FAIL"
    $verdictColor = "Red"
    $verdictIcon = "X"
}

Write-Host ""
Write-Host "   VERDICT:  [$verdictIcon] $verdict" -ForegroundColor $verdictColor
Write-Host ""
Write-Host "   Cassette:     $FolderName" -ForegroundColor White
Write-Host "   Files:        $($aviFiles.Count) AVI" -ForegroundColor White
Write-Host "   Playtime:     ${totalHours}h ${remMinutes}m ${remSeconds}s" -ForegroundColor White
Write-Host "   Size:         $([math]::Round($totalGB, 3)) GB" -ForegroundColor White
Write-Host "   Data rate:    $([math]::Round($gbPerMinute, 4)) GB/min" -ForegroundColor White
Write-Host "   DV errors:    $totalErrorFrames frame(s) / $totalFrames total" -ForegroundColor White
Write-Host "   Frozen:       $totalFreezes event(s), $([math]::Round($freezeTotalSec, 1))s" -ForegroundColor White
Write-Host "   Black:        $totalBlacks event(s), $([math]::Round($blackTotalSec, 1))s" -ForegroundColor White
Write-Host "   Silence:      $totalSilences event(s), $([math]::Round($silenceTotalSec, 1))s" -ForegroundColor White

if ($verdict -eq "PASS") {
    Write-Host ""
    Write-Host "   This cassette capture meets DV25 PAL archival quality standards." -ForegroundColor Green
    Write-Host "   No dropped frames, no concealment, no visual anomalies detected." -ForegroundColor Green
} elseif ($verdict -eq "PASS WITH WARNINGS") {
    Write-Host ""
    Write-Host "   Capture is usable but has minor issues noted above." -ForegroundColor Yellow
} elseif ($verdict -eq "FAIL") {
    Write-Host ""
    Write-Host "   This cassette has critical issues. Consider re-capturing" -ForegroundColor Red
    Write-Host "   problematic sections and using DVRescue merge to repair." -ForegroundColor Red
}

Write-Host ""
Write-Host "   Writing detailed cassette reports..." -ForegroundColor DarkGray

$runEndedAt = Get-Date
$fileReports = foreach ($detail in $fileDetails) {
    $stats = $perFileStats | Where-Object { $_.Name -eq $detail.Name } | Select-Object -First 1
    $fileFormatIssues = @($formatIssues | Where-Object { $_.File -eq $detail.Name })
    $fileDvIssues = @($dvIssues | Where-Object { $_.File -eq $detail.Name })
    $fileVisualIssues = @($visualIssues | Where-Object { $_.File -eq $detail.Name })
    $fileCriticals = @($fileFormatIssues + $fileDvIssues + $fileVisualIssues | Where-Object { $_.Level -eq "CRITICAL" })
    $fileWarnings = @($fileFormatIssues + $fileDvIssues + $fileVisualIssues | Where-Object { $_.Level -eq "WARNING" })
    $fileInfos = @($fileFormatIssues + $fileDvIssues + $fileVisualIssues | Where-Object { $_.Level -eq "INFO" })

    [PSCustomObject]@{
        Name      = $detail.Name
        FullPath  = if ($stats) { $stats.FullPath } else { Join-Path $FolderPath $detail.Name }
        Severity  = if ($fileCriticals.Count -gt 0) { "CRITICAL" } elseif ($fileWarnings.Count -gt 0) { "WARNING" } elseif ($fileInfos.Count -gt 0) { "INFO" } else { "OK" }
        Format    = $detail
        DV        = $stats
        Issues    = [PSCustomObject]@{
            Format = $fileFormatIssues
            DV     = $fileDvIssues
            Visual = $fileVisualIssues
        }
    }
}

$flatFileRows = foreach ($fr in $fileReports) {
    $stats = $fr.DV
    [PSCustomObject]@{
        Name                = $fr.Name
        FullPath            = $fr.FullPath
        Severity            = $fr.Severity
        DurationSeconds     = $fr.Format.Duration
        SizeMB              = $fr.Format.SizeMB
        VCodec              = $fr.Format.VCodec
        Width               = $fr.Format.Width
        Height              = $fr.Format.Height
        FPS                 = $fr.Format.FPS
        PixFmt              = $fr.Format.PixFmt
        VideoFrames         = $fr.Format.VFrames
        VideoPackets        = $fr.Format.VPackets
        ACodec              = $fr.Format.ACodec
        ASampleRate         = $fr.Format.ASampleRate
        BitRate             = $fr.Format.BitRate
        DVFrameCount        = if ($stats) { $stats.FrameCount } else { 0 }
        DVErrorFrames       = if ($stats) { $stats.ErrorFrames } else { 0 }
        DVErrorPct          = if ($stats) { $stats.ErrorPct } else { 0 }
        VideoSTA            = if ($stats) { $stats.VideoSTA } else { 0 }
        AudioData           = if ($stats) { $stats.AudioData } else { 0 }
        Concealed           = if ($stats) { $stats.Concealed } else { 0 }
        MidContentErrors    = if ($stats) { $stats.MidContentErrors } else { 0 }
        MidContentConcealed = if ($stats) { $stats.MidContentConcealed } else { 0 }
        TailErrors          = if ($stats) { $stats.TailErrors } else { 0 }
        TailConcealed       = if ($stats) { $stats.TailConcealed } else { 0 }
        TapeStartTC         = if ($stats) { $stats.TapeStartTC } else { $null }
        TapeStartRDT        = if ($stats) { $stats.TapeStartRDT } else { $null }
        TapeEndTC           = if ($stats) { $stats.TapeEndTC } else { $null }
        TapeEndRDT          = if ($stats) { $stats.TapeEndRDT } else { $null }
        FreezeEvents        = if ($stats) { $stats.FreezeEvents } else { 0 }
        FreezeSec           = if ($stats) { [math]::Round($stats.FreezeSec, 3) } else { 0 }
        PhotoEvents         = if ($stats -and $stats.PSObject.Properties.Name -contains "PhotoEvents") { $stats.PhotoEvents } else { 0 }
        PhotoSec            = if ($stats -and $stats.PSObject.Properties.Name -contains "PhotoSec") { [math]::Round($stats.PhotoSec, 3) } else { 0 }
        BlackEvents         = if ($stats) { $stats.BlackEvents } else { 0 }
        BlackSec            = if ($stats) { [math]::Round($stats.BlackSec, 3) } else { 0 }
        SilenceEvents       = if ($stats) { $stats.SilenceEvents } else { 0 }
        SilenceSec          = if ($stats) { [math]::Round($stats.SilenceSec, 3) } else { 0 }
        FormatIssueCount    = @($fr.Issues.Format).Count
        DVIssueCount        = @($fr.Issues.DV).Count
        VisualIssueCount    = @($fr.Issues.Visual).Count
    }
}

$summaryReport = [PSCustomObject]@{
    Tool              = "DV Cassette Archival Quality Checker"
    Cassette          = $FolderName
    FolderPath        = $FolderPath
    RunStartedAt      = $runStartedAt.ToString("o")
    RunEndedAt        = $runEndedAt.ToString("o")
    Verdict           = $verdict
    Files             = $aviFiles.Count
    PlaytimeSeconds   = [math]::Round($totalDuration, 3)
    PlaytimeMinutes   = [math]::Round($totalMinutes, 3)
    SizeBytes         = $totalSizeBytes
    SizeGB            = [math]::Round($totalGB, 6)
    DataRateGBPerMin  = [math]::Round($gbPerMinute, 6)
    CriticalCount     = $allCriticals.Count
    WarningCount      = $allWarnings.Count
    FormatIssues      = $formatIssues
    DVIssues          = $dvIssues
    VisualIssues      = $visualIssues
    Totals            = [PSCustomObject]@{
        FramesScanned              = $totalFrames
        ErrorFrames                = $totalErrorFrames
        MidContentErrors           = $totalMidContentErrors
        TailErrors                 = $totalTailErrors
        VideoSTAErrors             = $totalVideoSTAErrs
        AudioDataErrors            = $totalAudioDataErrs
        ConcealedFrames            = $totalConcealedFrames
        MidContentConcealedFrames  = $totalMidContentConcealed
        TailConcealedFrames        = $totalTailConcealed
        TimecodeBreaks             = $timecodeJumps
        FrozenEvents               = $totalFreezes
        FrozenSeconds              = [math]::Round($freezeTotalSec, 3)
        CameraPhotoModeEvents      = $totalPhotoMode
        CameraPhotoModeSeconds     = [math]::Round($photoModeTotalSec, 3)
        BlackEvents                = $totalBlacks
        BlackSeconds               = [math]::Round($blackTotalSec, 3)
        SilenceEvents              = $totalSilences
        SilenceSeconds             = [math]::Round($silenceTotalSec, 3)
    }
    FilesDetailed     = $fileReports
    FilesFlat         = $flatFileRows
}

try {
    $summaryReport | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $jsonReportPath -Encoding UTF8
    Write-Host "   JSON report: $jsonReportPath" -ForegroundColor DarkGray
} catch {
    Write-Host "   [WARNING] Could not write detailed cassette JSON report: $($_.Exception.Message)" -ForegroundColor Yellow
}

Write-Host "  $line" -ForegroundColor Cyan
Write-Host ""
