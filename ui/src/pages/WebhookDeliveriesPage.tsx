export default function WebhookDeliveriesPage() {
  return (
    <>
      <h1>Webhook Deliveries</h1>
      <div className="card">
        <p style={{ color: "var(--color-text-muted)" }}>
          This view surfaces the contents of the <code>webhook_delivery</code>{" "}
          queue: failed and exhausted deliveries plus a manual retry button.
          Wired up once the ops API is exposed — for now the dispatcher runs
          end-to-end against the schema.
        </p>
      </div>
    </>
  );
}
