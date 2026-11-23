; Inno Setup script: per-user install, no admin rights. Built by packaging\build_windows.ps1.
#ifndef AppVersion
  #define AppVersion "0.4.0"
#endif

[Setup]
AppId={{6E1F3B2A-9C4D-4F7E-8A51-2B7C0D9E4F31}
AppName=Lock & Land
AppVerName=Lock & Land {#AppVersion}
AppVersion={#AppVersion}
AppPublisher=Lock & Land (personal)
DefaultDirName={localappdata}\Programs\jobhunter
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=LockAndLand-setup-{#AppVersion}
SetupIconFile=..\build\assets\jobhunter.ico
UninstallDisplayIcon={app}\jobhunter.exe
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "autostart"; Description: "Start Lock && Land when I sign in to Windows (runs the background schedule)"; Flags: checkedonce
Name: "desktopicon"; Description: "Desktop shortcut"; Flags: unchecked

[Files]
Source: "..\dist\jobhunter\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; shortcuts from before the rename
Type: files; Name: "{userprograms}\jobhunter.lnk"
Type: files; Name: "{userdesktop}\jobhunter.lnk"

[Icons]
Name: "{userprograms}\Lock & Land"; Filename: "{app}\jobhunter.exe"
Name: "{userdesktop}\Lock & Land"; Filename: "{app}\jobhunter.exe"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "jobhunter"; ValueData: """{app}\jobhunter.exe"" --tray"; Tasks: autostart; Flags: uninsdeletevalue

[Run]
Filename: "{app}\jobhunter.exe"; Description: "Start Lock && Land now"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{cmd}"; Parameters: "/C taskkill /IM jobhunter.exe /F"; Flags: runhidden; RunOnceId: "StopApp"

[Code]
var
  DataPage: TInputDirWizardPage;

function PointerFile(): String;
begin
  Result := ExpandConstant('{userappdata}\jobhunter\home.txt');
end;

procedure InitializeWizard();
var
  Existing: AnsiString;
  Def: String;
begin
  Def := ExpandConstant('{localappdata}\jobhunter');
  if LoadStringFromFile(PointerFile(), Existing) then
    Def := Trim(String(Existing));
  DataPage := CreateInputDirPage(wpSelectDir, 'Data folder',
    'Where should Lock & Land keep your settings, CVs, letters and encrypted database?',
    'Pick a local folder (not OneDrive). Passwords and sign-ins never go here: they are stored in Windows Credential Manager.',
    False, '');
  DataPage.Add('');
  DataPage.Values[0] := Def;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
  begin
    ForceDirectories(DataPage.Values[0]);
    ForceDirectories(ExtractFileDir(PointerFile()));
    SaveStringToFile(PointerFile(), DataPage.Values[0], False);
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    MsgBox('Lock & Land was removed. Your data folder and the "jobhunter" entries in Windows Credential Manager were kept. ' +
           'Delete them yourself if you no longer need them (keep your recovery key if you might reinstall).', mbInformation, MB_OK);
end;
