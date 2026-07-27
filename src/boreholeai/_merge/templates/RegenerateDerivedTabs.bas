Attribute VB_Name = "modRegenerateTabs"
' Module name must differ from the Sub name below — if they match, Excel
' fails button assignment with "Formula is too complex to be assigned".
Option Explicit

' Rebuilds the Geology, Consistency_Density and USCS tabs from the Material
' tab, so reviewers can hand-correct Material and re-derive the other three
' without re-running the pipeline.
'
' VBA port of the BoreholeAI backend derivation
' (h_data_processing/data_processing_utils.py):
'   consolidate_intervals    - merge consecutive equal-value contiguous rows
'   drop_zero_thickness_rows - drop from=to rows, re-enforce contiguity
' Keep this module in sync with that Python logic.
'
' Deviation from Python: rows are grouped by (Source_File, Hole_ID) rather
' than Hole_ID alone, because a merged workbook can hold same-named holes
' from different source PDFs. Per-job workbooks behave identically.

Private Const MATERIAL_SHEET As String = "Material"

Public Sub RegenerateDerivedTabs()
    If MsgBox("Rewrite the Geology, Consistency_Density and USCS tabs from " & _
              "the Material tab?" & vbNewLine & vbNewLine & _
              "Manual edits made directly in those three tabs will be lost.", _
              vbYesNo + vbExclamation, "Regenerate derived tabs") <> vbYes Then Exit Sub

    ' Anything unexpected (protected sheets, co-authoring locks, ...) must
    ' end in a readable message, not a raw "Run-time error" dialog.
    On Error GoTo Failed

    Dim wsMat As Worksheet
    Set wsMat = SheetByName(MATERIAL_SHEET)
    If wsMat Is Nothing Then
        MsgBox "Sheet '" & MATERIAL_SHEET & "' was not found in this workbook.", _
               vbCritical, "Regenerate derived tabs"
        Exit Sub
    End If

    Dim data As Variant
    data = wsMat.UsedRange.Value
    If Not IsArray(data) Then
        MsgBox "The Material tab is empty.", vbCritical, "Regenerate derived tabs"
        Exit Sub
    End If

    Dim colHole As Long, colFrom As Long, colTo As Long, colSource As Long
    colHole = FindCol(data, "Hole_ID")
    colFrom = FindCol(data, "from_mbgl")
    colTo = FindCol(data, "to_mbgl")
    colSource = FindCol(data, "Source_File")   ' 0 when absent
    If colHole = 0 Or colFrom = 0 Or colTo = 0 Then
        MsgBox "The Material tab is missing one of the required columns " & _
               "Hole_ID / from_mbgl / to_mbgl.", vbCritical, "Regenerate derived tabs"
        Exit Sub
    End If

    Dim nData As Long
    nData = UBound(data, 1) - 1                ' data rows (row 1 is the header)
    Dim order() As Long
    If nData > 0 Then
        order = SortedRowOrder(data, nData, colSource, colHole, colFrom)
    End If

    Dim tabNames As Variant, valueCols As Variant
    tabNames = Array("Geology", "Consistency_Density", "USCS")
    valueCols = Array("geology_type", "consistency_density_strength", "uscs")

    Dim prevSheet As Worksheet
    Set prevSheet = ActiveSheet
    Application.ScreenUpdating = False

    ' New sheets are inserted after the previous tab in the canonical order
    ' Material -> Geology -> Consistency_Density -> USCS.
    Dim summary As String, anchor As String, i As Long
    anchor = MATERIAL_SHEET
    For i = LBound(tabNames) To UBound(tabNames)
        Dim vCol As Long
        vCol = FindCol(data, CStr(valueCols(i)))
        If vCol = 0 Then
            summary = summary & tabNames(i) & ": skipped - column '" & _
                      valueCols(i) & "' not found in Material" & vbNewLine
            If Not SheetByName(CStr(tabNames(i))) Is Nothing Then anchor = CStr(tabNames(i))
        Else
            Dim written As Long
            written = RebuildTab(CStr(tabNames(i)), anchor, data, order, nData, _
                                 colSource, colHole, colFrom, colTo, vCol, _
                                 CStr(valueCols(i)))
            summary = summary & tabNames(i) & ": " & written & " row(s)" & vbNewLine
            anchor = CStr(tabNames(i))
        End If
    Next i

    prevSheet.Activate
    Application.ScreenUpdating = True

    MsgBox "Done." & vbNewLine & vbNewLine & summary, vbInformation, _
           "Regenerate derived tabs"
    Exit Sub

Failed:
    Application.ScreenUpdating = True
    Dim msg As String
    msg = "Could not rewrite the tabs: " & Err.Description & vbNewLine & vbNewLine & _
          "If any sheet is protected, unprotect it (Review > Unprotect Sheet) " & _
          "and click the button again."
    If Len(summary) > 0 Then
        msg = msg & vbNewLine & vbNewLine & "Completed before the error:" & _
              vbNewLine & summary
    End If
    MsgBox msg, vbCritical, "Regenerate derived tabs"
End Sub




' -------------------------------------------
' Internal Helper Functions
' -------------------------------------------

Private Function RebuildTab(tabName As String, anchorSheet As String, _
                            data As Variant, order() As Long, nData As Long, _
                            colSource As Long, colHole As Long, _
                            colFrom As Long, colTo As Long, vCol As Long, _
                            valueName As String) As Long
    ' --- consolidate_intervals ------------------------------------------
    Dim outKey() As String, outSource() As Variant, outHole() As Variant
    Dim outFrom() As Variant, outTo() As Variant, outVal() As Variant
    Dim n As Long
    If nData > 0 Then
        ReDim outKey(1 To nData): ReDim outSource(1 To nData)
        ReDim outHole(1 To nData): ReDim outFrom(1 To nData)
        ReDim outTo(1 To nData): ReDim outVal(1 To nData)
    End If

    Dim k As Long, r As Long, curKey As String
    For k = 1 To nData
        r = order(k)
        curKey = GroupKey(data, r, colSource, colHole)
        If n > 0 Then
            If curKey = outKey(n) _
               And Not IsBlank(outVal(n)) And Not IsBlank(data(r, vCol)) _
               And ValuesEqual(outVal(n), data(r, vCol)) _
               And ValuesEqual(outTo(n), data(r, colFrom)) Then
                outTo(n) = data(r, colTo)      ' extend the previous interval
                GoTo NextRow
            End If
        End If
        n = n + 1
        outKey(n) = curKey
        If colSource > 0 Then outSource(n) = data(r, colSource)
        outHole(n) = data(r, colHole)
        outFrom(n) = data(r, colFrom)
        outTo(n) = data(r, colTo)
        outVal(n) = data(r, vCol)
NextRow:
    Next k

    ' --- drop_zero_thickness_rows: drop from = to, re-enforce contiguity -
    Dim keep() As Long, m As Long
    If n > 0 Then ReDim keep(1 To n)
    For k = 1 To n
        If Not DepthsEqual(outFrom(k), outTo(k)) Then
            m = m + 1
            keep(m) = k
        End If
    Next k
    For k = 2 To m
        If outKey(keep(k)) = outKey(keep(k - 1)) Then
            outFrom(keep(k)) = outTo(keep(k - 1))
        End If
    Next k

    ' --- write the sheet -------------------------------------------------
    Dim headers As Variant, nCols As Long
    If colSource > 0 Then
        headers = Array("Source_File", "Hole_ID", "from_mbgl", "to_mbgl", valueName)
    Else
        headers = Array("Hole_ID", "from_mbgl", "to_mbgl", valueName)
    End If
    nCols = UBound(headers) + 1

    Dim ws As Worksheet
    Set ws = SheetByName(tabName)
    If ws Is Nothing Then
        Set ws = ThisWorkbook.Worksheets.Add( _
            After:=ThisWorkbook.Worksheets(anchorSheet))
        ws.Name = tabName
    Else
        ws.Rows("2:" & ws.Rows.Count).Delete
    End If
    ws.Range(ws.Cells(1, 1), ws.Cells(1, nCols)).Value = headers
    If ws.UsedRange.Columns.Count > nCols Then
        ws.Range(ws.Cells(1, nCols + 1), _
                 ws.Cells(1, ws.UsedRange.Columns.Count)).Clear
    End If

    If m > 0 Then
        Dim outArr() As Variant, c As Long
        ReDim outArr(1 To m, 1 To nCols)
        For k = 1 To m
            c = 0
            If colSource > 0 Then c = c + 1: outArr(k, c) = outSource(keep(k))
            c = c + 1: outArr(k, c) = outHole(keep(k))
            c = c + 1: outArr(k, c) = outFrom(keep(k))
            c = c + 1: outArr(k, c) = outTo(keep(k))
            c = c + 1: outArr(k, c) = outVal(keep(k))
        Next k
        ws.Range(ws.Cells(2, 1), ws.Cells(m + 1, nCols)).Value = outArr
    End If

    StyleSheet ws, m, nCols, headers
    RebuildTab = m
End Function

Private Sub StyleSheet(ws As Worksheet, nRowsData As Long, nCols As Long, _
                       headers As Variant)
    ' Mirrors _apply_styles in the SDK merge (_merge/_excel.py).
    Dim c As Long, r As Long

    With ws.Range(ws.Cells(1, 1), ws.Cells(1, nCols))
        .Interior.Color = RGB(68, 114, 196)          ' 4472C4 header blue
        .Font.Bold = True
        .Font.Color = RGB(255, 255, 255)
        .Font.Size = 11
        .HorizontalAlignment = xlCenter
        .VerticalAlignment = xlCenter
        .WrapText = True
        .Borders.LineStyle = xlContinuous
        .Borders.Weight = xlMedium
        .Borders.Color = RGB(68, 114, 196)
    End With
    ws.Rows(1).RowHeight = 30

    If nRowsData > 0 Then
        Dim dataRng As Range
        Set dataRng = ws.Range(ws.Cells(2, 1), ws.Cells(nRowsData + 1, nCols))
        With dataRng.Borders
            .LineStyle = xlContinuous
            .Weight = xlThin
            .Color = RGB(184, 204, 228)              ' B8CCE4 data border
        End With
        dataRng.VerticalAlignment = xlCenter
        For c = 1 To nCols
            If IsLeftAligned(CStr(headers(c - 1))) Then
                dataRng.Columns(c).HorizontalAlignment = xlLeft
            Else
                dataRng.Columns(c).HorizontalAlignment = xlCenter
            End If
        Next c
        For r = 2 To nRowsData + 1
            If r Mod 2 = 0 Then
                ws.Range(ws.Cells(r, 1), ws.Cells(r, nCols)).Interior.Color = _
                    RGB(248, 249, 250)               ' F8F9FA alt-row fill
            End If
        Next r
    End If

    ' Column widths: max text length + 2, capped at 60 (same as the merge).
    For c = 1 To nCols
        Dim maxLen As Long, v As Variant
        maxLen = Len(CStr(headers(c - 1)))
        For r = 2 To nRowsData + 1
            v = ws.Cells(r, c).Value
            If Not IsError(v) Then
                If Len(CStr(v)) > maxLen Then maxLen = Len(CStr(v))
            End If
        Next r
        Dim w As Long
        w = maxLen + 2
        If w > 60 Then w = 60
        ws.Columns(c).ColumnWidth = w
    Next c

    ws.Activate
    ActiveWindow.FreezePanes = False
    ws.Range("A2").Select
    ActiveWindow.FreezePanes = True
End Sub

Private Function SortedRowOrder(data As Variant, nData As Long, _
                                colSource As Long, colHole As Long, _
                                colFrom As Long) As Long()
    ' Row order for consolidation: groups in first-appearance order
    ' (groupby sort=False), each group stable-sorted by from_mbgl ascending
    ' with non-numeric from_mbgl last (sort_values NaN placement).
    Dim keyOrd As Collection
    Set keyOrd = New Collection
    Dim ords() As Long, fromV() As Double
    ReDim ords(1 To nData): ReDim fromV(1 To nData)

    Dim i As Long, r As Long, o As Long, nGroups As Long
    For i = 1 To nData
        r = i + 1
        o = OrdinalFor(keyOrd, GroupKey(data, r, colSource, colHole))
        If o = 0 Then
            nGroups = nGroups + 1
            keyOrd.Add nGroups, GroupKey(data, r, colSource, colHole)
            o = nGroups
        End If
        ords(i) = o
        If IsBlank(data(r, colFrom)) Or Not IsNumeric(data(r, colFrom)) Then
            fromV(i) = 1E+300
        Else
            fromV(i) = CDbl(data(r, colFrom))
        End If
    Next i

    Dim order() As Long
    ReDim order(1 To nData)
    For i = 1 To nData: order(i) = i + 1: Next i

    ' Stable insertion sort by (group ordinal, from_mbgl).
    Dim j As Long, cur As Long, curOrd As Long, curFrom As Double
    For i = 2 To nData
        cur = order(i): curOrd = ords(cur - 1): curFrom = fromV(cur - 1)
        j = i - 1
        Do While j >= 1
            If ords(order(j) - 1) > curOrd Or _
               (ords(order(j) - 1) = curOrd And fromV(order(j) - 1) > curFrom) Then
                order(j + 1) = order(j)
                j = j - 1
            Else
                Exit Do
            End If
        Loop
        order(j + 1) = cur
    Next i
    SortedRowOrder = order
End Function

Private Function OrdinalFor(col As Collection, k As String) As Long
    ' Collection has no Exists test; the error trap is the standard idiom.
    On Error Resume Next
    OrdinalFor = col(k)
    On Error GoTo 0
End Function

Private Function GroupKey(data As Variant, r As Long, colSource As Long, _
                          colHole As Long) As String
    Dim s As String
    If colSource > 0 Then s = SafeStr(data(r, colSource))
    GroupKey = s & "|" & SafeStr(data(r, colHole))
End Function

Private Function SheetByName(name As String) As Worksheet
    Dim ws As Worksheet
    For Each ws In ThisWorkbook.Worksheets
        If ws.Name = name Then
            Set SheetByName = ws
            Exit Function
        End If
    Next ws
End Function

Private Function FindCol(data As Variant, header As String) As Long
    Dim c As Long
    For c = 1 To UBound(data, 2)
        If Not IsError(data(1, c)) Then
            If CStr(data(1, c)) = header Then
                FindCol = c
                Exit Function
            End If
        End If
    Next c
End Function

Private Function SafeStr(v As Variant) As String
    If IsError(v) Then
        SafeStr = "#ERROR"
    ElseIf IsEmpty(v) Or IsNull(v) Then
        SafeStr = ""
    Else
        SafeStr = Trim$(CStr(v))
    End If
End Function

Private Function IsBlank(v As Variant) As Boolean
    IsBlank = (SafeStr(v) = "")
End Function

Private Function ValuesEqual(a As Variant, b As Variant) As Boolean
    ' Case-sensitive, like Python ==. Blank never equals anything (NaN rule).
    If IsBlank(a) Or IsBlank(b) Then Exit Function
    If IsNumeric(a) And IsNumeric(b) Then
        ValuesEqual = (CDbl(a) = CDbl(b))
    Else
        ValuesEqual = (SafeStr(a) = SafeStr(b))
    End If
End Function

Private Function DepthsEqual(a As Variant, b As Variant) As Boolean
    ' Only a real numeric pair can be equal (NaN != NaN keeps blank rows).
    If IsBlank(a) Or IsBlank(b) Then Exit Function
    If IsNumeric(a) And IsNumeric(b) Then
        DepthsEqual = (CDbl(a) = CDbl(b))
    End If
End Function

Private Function IsLeftAligned(colName As String) As Boolean
    ' Subset of _LEFT_ALIGN_COLS relevant to the derived tabs.
    IsLeftAligned = (colName = "Source_File" Or colName = "geology_type" _
                     Or colName = "consistency_density_strength")
End Function
