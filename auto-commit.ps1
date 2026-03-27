$projectPath = "C:\Users\qalle\OneDrive\Documenten\ClaudeProjects"
Set-Location $projectPath

$status = git status --porcelain
if ($status) {
    git add -A
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm"
    git commit -m "Auto-save: $timestamp"
    git push origin main
}
