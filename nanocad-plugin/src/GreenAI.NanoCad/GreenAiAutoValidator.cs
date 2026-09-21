using HostMgd.ApplicationServices;
using Teigha.DatabaseServices;
using HostApplication = HostMgd.ApplicationServices.Application;

namespace GreenAI.NanoCad;

/// <summary>
/// Watches edits to GreenAI planting circles.  A normal nanoCAD MOVE/COPY/
/// ERASE action is followed by the same validation used by the editor buttons.
/// </summary>
internal static class GreenAiAutoValidator
{
    private static readonly HashSet<string> EditingCommands = new(StringComparer.OrdinalIgnoreCase)
    {
        "MOVE", "COPY", "ERASE", "STRETCH", "GRIP_STRETCH", "ROTATE", "SCALE", "PASTECLIP"
    };
    private static readonly HashSet<Database> DirtyDatabases = new();
    private static readonly HashSet<Document> AttachedDocuments = new();

    public static void Initialize()
    {
        HostApplication.DocumentManager.DocumentCreated += OnDocumentCreated;
        var active = HostApplication.DocumentManager.MdiActiveDocument;
        if (active is not null)
            Attach(active);
    }

    public static void Terminate()
    {
        HostApplication.DocumentManager.DocumentCreated -= OnDocumentCreated;
        foreach (var document in AttachedDocuments.ToArray())
            Detach(document);
        DirtyDatabases.Clear();
    }

    private static void OnDocumentCreated(object sender, DocumentCollectionEventArgs args) =>
        Attach(args.Document);

    private static void Attach(Document document)
    {
        if (!AttachedDocuments.Add(document))
            return;
        document.Database.ObjectModified += OnObjectChanged;
        document.Database.ObjectAppended += OnObjectChanged;
        document.Database.ObjectErased += OnObjectErased;
        document.CommandEnded += OnCommandEnded;
    }

    private static void Detach(Document document)
    {
        if (!AttachedDocuments.Remove(document))
            return;
        document.Database.ObjectModified -= OnObjectChanged;
        document.Database.ObjectAppended -= OnObjectChanged;
        document.Database.ObjectErased -= OnObjectErased;
        document.CommandEnded -= OnCommandEnded;
    }

    private static void OnObjectChanged(object sender, ObjectEventArgs args)
    {
        try
        {
            if (IsPlantingEntity(args.DBObject))
                DirtyDatabases.Add(args.DBObject.Database);
        }
        catch
        {
            // A database notification must never interrupt the nanoCAD command.
        }
    }

    private static void OnObjectErased(object sender, ObjectErasedEventArgs args)
    {
        try
        {
            if (IsPlantingEntity(args.DBObject))
                DirtyDatabases.Add(args.DBObject.Database);
        }
        catch
        {
            // Erased entities may no longer expose all properties.
        }
    }

    private static bool IsPlantingEntity(DBObject value) =>
        value is Entity entity &&
        (entity.Layer.Equals("GREEN_AI_PLANT_TREE", StringComparison.OrdinalIgnoreCase) ||
         entity.Layer.Equals("GREEN_AI_PLANT_SHRUB", StringComparison.OrdinalIgnoreCase));

    private static void OnCommandEnded(object sender, CommandEventArgs args)
    {
        if (sender is not Document document || !DirtyDatabases.Remove(document.Database))
            return;
        if (!GreenAiSession.AutoValidateEdits ||
            !EditingCommands.Contains(args.GlobalCommandName) ||
            args.GlobalCommandName.StartsWith("GREENAI_", StringComparison.OrdinalIgnoreCase))
            return;
        GreenAiPanel.SetStatus("Посадка изменена — запускаю автоматическую проверку…");
        GreenAiPanel.Log($"Изменение командой {args.GlobalCommandName}: запущена автопроверка.");
        document.SendStringToExecute("GREENAI_CHECK ", true, false, false);
    }
}
