#ifndef AppVersion
  #define AppVersion "0.1.0"
#endif

#ifndef SourceDir
  #error SourceDir must be defined. Pass /DSourceDir=path-to-cua-win.dist.
#endif

#ifndef OutputDir
  #define OutputDir "."
#endif

#ifndef SetupBaseName
  #define SetupBaseName "cua-win-" + AppVersion + "-win64-setup"
#endif

[Setup]
AppId={{B99CB8B0-5DE5-4A0B-AEF9-69A6A3F64668}
AppName=cua-win
AppVersion={#AppVersion}
AppPublisher=zhangweiooy
AppPublisherURL=https://github.com/zhangweiooy/cua-pywin
AppSupportURL=https://github.com/zhangweiooy/cua-pywin/issues
AppUpdatesURL=https://github.com/zhangweiooy/cua-pywin/releases
DefaultDirName={localappdata}\Programs\cua-win
DefaultGroupName=cua-win
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDir}
OutputBaseFilename={#SetupBaseName}
UninstallDisplayIcon={app}\cua-win.exe
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
SetupLogging=yes
ChangesEnvironment=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "addtopath"; Description: "Add cua-win to the current user's PATH"; GroupDescription: "Command line integration:"; Flags: checkedonce

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
Type: files; Name: "{app}\computer_server\models\omniparser_v2\icon_caption_onnx_community\onnx\vision_encoder.onnx"
Type: files; Name: "{app}\som\weights\icon_detect\model.pt"
Type: filesandordirs; Name: "{app}\easyocr"
Type: filesandordirs; Name: "{app}\easyocr-*.dist-info"
Type: filesandordirs; Name: "{app}\torch"
Type: filesandordirs; Name: "{app}\torch-*.dist-info"
Type: filesandordirs; Name: "{app}\torchvision"
Type: filesandordirs; Name: "{app}\torchvision-*.dist-info"
Type: filesandordirs; Name: "{app}\ultralytics"
Type: filesandordirs; Name: "{app}\ultralytics-*.dist-info"
Type: filesandordirs; Name: "{app}\ultralytics_thop"
Type: filesandordirs; Name: "{app}\ultralytics_thop-*.dist-info"

[UninstallDelete]
Type: dirifempty; Name: "{app}\computer_server\models\omniparser_v2\icon_caption_onnx_community\onnx"
Type: dirifempty; Name: "{app}\computer_server\models\omniparser_v2\icon_caption_onnx_community"
Type: dirifempty; Name: "{app}\computer_server\models\omniparser_v2"
Type: dirifempty; Name: "{app}\computer_server\models"
Type: dirifempty; Name: "{app}\computer_server"
Type: dirifempty; Name: "{app}\som\weights\icon_detect"
Type: dirifempty; Name: "{app}\som\weights"
Type: dirifempty; Name: "{app}\som"
Type: dirifempty; Name: "{app}"

[Icons]
Name: "{autoprograms}\cua-win\cua-win"; Filename: "{app}\cua-win.exe"; WorkingDir: "{app}"
Name: "{autoprograms}\cua-win\cua-win doctor"; Filename: "{app}\cua-win.exe"; Parameters: "doctor"; WorkingDir: "{app}"
Name: "{autoprograms}\cua-win\Start daemon"; Filename: "{app}\cua-win.exe"; Parameters: "start"; WorkingDir: "{app}"
Name: "{autoprograms}\cua-win\Stop daemon"; Filename: "{app}\cua-win.exe"; Parameters: "stop"; WorkingDir: "{app}"

[Run]
Filename: "{cmd}"; Parameters: "/K ""{app}\cua-win.exe"" doctor --no-daemon"; Description: "Run cua-win doctor"; Flags: postinstall skipifsilent unchecked

[Code]
const
  EnvKey = 'Environment';

function NormalizePath(Value: string): string;
begin
  Result := Value;
  while (Length(Result) > 3) and (Copy(Result, Length(Result), 1) = '\') do
  begin
    Delete(Result, Length(Result), 1);
  end;
end;

function PathContainsDir(PathValue: string; Dir: string): Boolean;
var
  Remaining: string;
  Item: string;
  Semi: Integer;
begin
  Result := False;
  Remaining := PathValue;
  Dir := NormalizePath(Dir);

  while Remaining <> '' do
  begin
    Semi := Pos(';', Remaining);
    if Semi > 0 then
    begin
      Item := Copy(Remaining, 1, Semi - 1);
      Delete(Remaining, 1, Semi);
    end
    else
    begin
      Item := Remaining;
      Remaining := '';
    end;

    if (Item <> '') and (CompareText(NormalizePath(Item), Dir) = 0) then
    begin
      Result := True;
      Exit;
    end;
  end;
end;

function AppendPathPart(Current: string; Item: string): string;
begin
  if Item = '' then
  begin
    Result := Current;
  end
  else if Current = '' then
  begin
    Result := Item;
  end
  else
  begin
    Result := Current + ';' + Item;
  end;
end;

function RemoveDirFromPath(PathValue: string; Dir: string): string;
var
  Remaining: string;
  Item: string;
  Semi: Integer;
  HasMore: Boolean;
  FirstOutput: Boolean;
begin
  Result := '';
  Remaining := PathValue;
  Dir := NormalizePath(Dir);
  FirstOutput := True;

  repeat
    HasMore := False;
    Semi := Pos(';', Remaining);
    if Semi > 0 then
    begin
      Item := Copy(Remaining, 1, Semi - 1);
      Delete(Remaining, 1, Semi);
      HasMore := True;
    end
    else
    begin
      Item := Remaining;
      Remaining := '';
    end;

    if CompareText(NormalizePath(Item), Dir) <> 0 then
    begin
      if not FirstOutput then
      begin
        Result := Result + ';';
      end;
      Result := Result + Item;
      FirstOutput := False;
    end;
  until not HasMore;
end;

procedure AddToUserPath(Dir: string);
var
  PathValue: string;
begin
  if not RegQueryStringValue(HKCU, EnvKey, 'Path', PathValue) then
  begin
    PathValue := '';
  end;

  if not PathContainsDir(PathValue, Dir) then
  begin
    RegWriteStringValue(HKCU, EnvKey, 'Path', AppendPathPart(PathValue, Dir));
  end;
end;

procedure RemoveFromUserPath(Dir: string);
var
  PathValue: string;
  NewPathValue: string;
begin
  if RegQueryStringValue(HKCU, EnvKey, 'Path', PathValue) then
  begin
    NewPathValue := RemoveDirFromPath(PathValue, Dir);
    if NewPathValue <> PathValue then
    begin
      RegWriteStringValue(HKCU, EnvKey, 'Path', NewPathValue);
    end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and IsTaskSelected('addtopath') then
  begin
    AddToUserPath(ExpandConstant('{app}'));
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    RemoveFromUserPath(ExpandConstant('{app}'));
    RemoveDir(ExpandConstant('{app}'));
  end;
end;
